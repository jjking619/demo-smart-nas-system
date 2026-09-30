#!/usr/bin/env python3
import argparse
import datetime as dt
import math
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import numpy as np
import sherpa_onnx

CONFORMER_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-conformer-zh-stateless2-2023-05-23.tar.bz2"
)
SENSEVOICE_MODEL_URL = (
	"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
	"sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
)
SENSEVOICE_MODEL_DIRNAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

MATCHA_ZH_EN_TTS_URL = (
	"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
	"matcha-icefall-zh-en.tar.bz2"
)
VOCODER_16KHZ_URL = (
	"https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/"
	"vocos-16khz-univ.onnx"
)
OFFICIAL_MATCHA_DIRNAME = "matcha-icefall-zh-en"

DEFAULT_HOTWORDS = [
	"小远同学", "xiaoyuan", "小远", "小元同学", "小园同学", "小源同学", "小袁同学",
    "家庭相册", "手机相册", "工作文档", "备份", "旅行",
    "图片", "照片", "文件夹", "文件", "视频",
    "分类", "移动到", "新建", "删除", "重命名",
    "打开", "复制", "所有", "全部", "播放",
	"下载", "测试", "海洋", "兔子", "预告片",
	"电影", "电视剧", "音乐", "歌曲", "剧集", "家庭影院", "Movies", "TV Shows", "movie", "series", "保存",
	"复古", "日系", "胶片", "滤镜", "风格",
    "合同", "住房合同", "预算", "方案", "协议", "表格", "报告", "记录",
    "租房", "房产",
]


def default_hotwords_path() -> Path:
    return Path(__file__).resolve().parent / "hotwords.txt"


def ensure_hotwords_file(path: Path = None) -> Path:
	"""Create the hotwords file if it does not exist."""
	path = path or default_hotwords_path()
	if not path.exists():
		path.write_text("\n".join(DEFAULT_HOTWORDS) + "\n", encoding="utf-8")
		print(f"[ASR] hotwords file created: {path}")
	return path


def _is_cjk_hotword(word: str) -> bool:
	"""Keep only Han-character hotwords for cjkchar encoding."""
	return bool(word) and all("\u4e00" <= ch <= "\u9fff" for ch in word)


def hotwords_for_cjkchar(path: Path) -> Path:
	"""Filter hotwords to the subset that cjkchar can encode."""
	if not path.exists():
		return path

	try:
		raw_lines = path.read_text(encoding="utf-8").splitlines()
	except OSError:
		return path

	kept: list[str] = []
	dropped: list[str] = []
	seen: set[str] = set()
	for line in raw_lines:
		item = line.strip()
		if not item or item.startswith("#"):
			continue
		word = item.split(":", 1)[0].strip()
		if not _is_cjk_hotword(word):
			dropped.append(word or item)
			continue
		if word in seen:
			continue
		seen.add(word)
		kept.append(item)

	cache_dir = Path(tempfile.gettempdir()) / "voice_bridge_hotwords"
	try:
		cache_dir.mkdir(parents=True, exist_ok=True)
	except OSError:
		return path
	out = cache_dir / f"{path.stem}.cjkchar{path.suffix or '.txt'}"
	content = "\n".join(kept) + ("\n" if kept else "")
	try:
		if not out.exists() or out.read_text(encoding="utf-8") != content:
			out.write_text(content, encoding="utf-8")
	except OSError:
		return path

	if dropped:
		print(
			f"[ASR] hotwords: dropped {len(dropped)} non-CJK entries "
			f"(cjkchar cannot encode them): {', '.join(dropped)}"
		)
	return out


def _parse_wake_words(value: str) -> list[str]:
    parts = re.split(r"[,，;；\n]+", value or "")
    out = []
    seen = set()
    for part in parts:
        w = part.strip()
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _normalize_for_wake(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)
    return text


def _detect_wakeup_open_asr(recognizer, wav_path: Path, wake_words: list[str], engine: str):
    text = asr_transcribe(recognizer, wav_path, engine=engine)
    norm_text = _normalize_for_wake(text)
    for word in wake_words:
        norm_word = _normalize_for_wake(word)
        if norm_word and norm_word in norm_text:
            return True, word, text
    return False, "", text


def _resolve_sdk_root(folder_name: str) -> Path:
	env_base = os.getenv("VOICE_SDK_BASE")
	candidates = []
	if env_base:
		candidates.append(Path(env_base) / folder_name)

	candidates.append(Path(__file__).resolve().parent.parent / folder_name)
	candidates.append(Path("/home/pi/voice") / folder_name)

	for p in candidates:
		if p.exists():
			return p
	return candidates[0]


def check_cmd_exists(name: str) -> None:
	if shutil.which(name) is None:
		raise RuntimeError(f"Missing required command: {name}")


def run_cmd(cmd, cwd=None, env=None, input_text=None):
	return subprocess.run(
		cmd,
		cwd=cwd,
		env=env,
		input=input_text,
		text=True,
		capture_output=True,
		check=False,
	)


def record_audio_with_ffmpeg(
	out_wav: Path, duration: float, mic_input: str, backend: str
) -> None:
	base = [
		"ffmpeg",
		"-y",
		"-f",
		backend,
		"-i",
		mic_input,
		"-ac",
		"1",
		"-ar",
		"16000",
		"-t",
		str(duration),
		str(out_wav),
		"-loglevel",
		"error",
	]
	p = run_cmd(base)
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffmpeg record failed")

	# Some audio backends can occasionally return success but write a near-empty WAV.
	# Reject such clips so caller fallback/retry logic can pick a healthier source.
	try:
		import wave

		with wave.open(str(out_wav), "rb") as wf:
			sr = wf.getframerate() or 16000
			n_frames = wf.getnframes()
			duration_sec = float(n_frames) / float(sr)

		min_valid_sec = max(0.12, min(0.8, float(duration) * 0.2))
		if duration_sec < min_valid_sec:
			raise RuntimeError(
				f"Recorded clip too short: {duration_sec:.3f}s "
				f"(expect >= {min_valid_sec:.3f}s, backend={backend}, mic={mic_input})"
			)
	except RuntimeError:
		raise
	except Exception as e:  # noqa: BLE001
		raise RuntimeError(
			f"Recorded WAV validation failed (backend={backend}, mic={mic_input}): {e}"
		)


MIN_RECORDABLE_CHUNK_SEC = 0.3


def _env_bool(name: str, default: bool) -> bool:
	value = os.getenv(name)
	if value is None:
		return default
	v = value.strip().lower()
	if v in {"1", "true", "yes", "y", "on"}:
		return True
	if v in {"0", "false", "no", "n", "off"}:
		return False
	return default


def _env_optional_float(name: str) -> float | None:
	value = os.getenv(name)
	if value is None or not value.strip():
		return None
	try:
		return float(value)
	except ValueError:
		return None


class MicOption:
	__slots__ = ("name", "backend", "device")

	def __init__(self, name: str, backend: str, device: str):
		self.name = (name or "").strip() or "custom"
		self.backend = (backend or "").strip().lower()
		self.device = (device or "").strip()

	def label(self) -> str:
		return f"{self.name}({self.backend}:{self.device})"


def build_mic_options(
	priority: str,
	usb_backend: str,
	usb_input: str,
	onboard_backend: str,
	onboard_input: str,
) -> list[MicOption]:
	named = {
		"usb": MicOption("usb", usb_backend, usb_input),
		"onboard": MicOption("onboard", onboard_backend, onboard_input),
	}
	options: list[MicOption] = []
	seen = set()
	for raw in (priority or "").split(","):
		entry = raw.strip()
		if not entry:
			continue
		opt = named.get(entry.lower())
		if opt is None and ":" in entry:
			backend, _, device = entry.partition(":")
			opt = MicOption("custom", backend, device)
		if opt is None:
			print(f"[MIC] unknown priority entry ignored: {entry!r}")
			continue
		if not opt.device:
			print(f"[MIC] priority entry skipped (missing device): {entry!r}")
			continue
		sig = (opt.backend, opt.device)
		if sig in seen:
			continue
		seen.add(sig)
		options.append(opt)
	return options


def build_legacy_mic_options(mic_input: str, backend: str) -> list[MicOption]:
	primary = (mic_input or "").strip()
	backend = (backend or "auto").strip().lower()
	options: list[MicOption] = []
	if primary:
		options.append(MicOption("usb", "pulse" if backend == "pulse" else "alsa", primary))
	if backend != "pulse" or not primary:
		options.append(MicOption("onboard", "pulse", "regular0"))
	return options


class MicResolver:
	def __init__(
		self,
		options: list[MicOption],
		min_level_dbfs: float | None = None,
		strict: bool = False,
		recheck_sec: float = 60.0,
		probe_sec: float = 0.4,
		probe_dir: Path | None = None,
		fail_threshold: int = 2,
	):
		self.options = list(options or [])
		self.min_level_dbfs = min_level_dbfs
		self.strict = bool(strict)
		self.recheck_sec = max(0.0, float(recheck_sec))
		self.probe_sec = max(0.2, float(probe_sec))
		self.probe_dir = Path(probe_dir) if probe_dir else Path(tempfile.gettempdir()) / "voice_bridge"
		self.fail_threshold = max(1, int(fail_threshold))
		self._current: MicOption | None = None
		self._consecutive_failures = 0
		self._last_recheck_ts = 0.0

	def selected(self) -> MicOption | None:
		return self._current

	def _probe(self, opt: MicOption) -> str | None:
		if not opt.device:
			return "empty device"
		self.probe_dir.mkdir(parents=True, exist_ok=True)
		probe_wav = self.probe_dir / f"mic_probe_{os.getpid()}.wav"
		try:
			record_audio_with_ffmpeg(probe_wav, self.probe_sec, opt.device, opt.backend)
			if self.min_level_dbfs is not None:
				level = wav_level_dbfs(probe_wav)
				if level < self.min_level_dbfs:
					return f"level {level:.1f} dBFS < min {self.min_level_dbfs:.1f} dBFS"
			return None
		except Exception as e:  # noqa: BLE001
			text = str(e).strip()
			return text.splitlines()[0] if text else repr(e)
		finally:
			probe_wav.unlink(missing_ok=True)

	def _select(self, reason: str) -> None:
		candidates = self.options[:1] if self.strict else self.options
		if not candidates:
			self._current = None
			raise RuntimeError(f"No microphone configured ({reason})")

		failures: list[str] = []
		for opt in candidates:
			err = self._probe(opt)
			if err is None:
				prev = self._current
				self._current = opt
				self._consecutive_failures = 0
				self._last_recheck_ts = time.time()
				if prev is not None and prev.label() != opt.label():
					print(f"[MIC] switched: {prev.label()} -> {opt.label()} ({reason})")
				else:
					print(f"[MIC] selected: {opt.label()} ({reason})")
				print(f"[MIC] exclusive: using {opt.label()}, other mics stay closed")
				return
			failures.append(f"{opt.label()}: {err}")

		self._current = None
		raise RuntimeError(f"No usable microphone ({reason}): " + " | ".join(failures))

	def maybe_recheck(self) -> None:
		if self._current is None or not self.options:
			return
		preferred = self.options[0]
		if self._current.label() == preferred.label():
			return
		if self.recheck_sec > 0 and (time.time() - self._last_recheck_ts) < self.recheck_sec:
			return
		self._last_recheck_ts = time.time()
		if self._probe(preferred) is None:
			print(f"[MIC] preferred mic is back: {preferred.label()}")
			self._current = preferred
			self._consecutive_failures = 0

	def record(self, out_wav: Path, duration: float) -> MicOption:
		self.maybe_recheck()
		if self._current is None:
			self._select("initial")

		try:
			record_audio_with_ffmpeg(out_wav, duration, self._current.device, self._current.backend)
			self._consecutive_failures = 0
			return self._current
		except Exception as e:  # noqa: BLE001
			self._consecutive_failures += 1
			print(
				f"[MIC] record failed on {self._current.label()} "
				f"({self._consecutive_failures}/{self.fail_threshold}): {e}"
			)
			if self._consecutive_failures < self.fail_threshold:
				raise

		self._select("re-probe after failures")
		record_audio_with_ffmpeg(out_wav, duration, self._current.device, self._current.backend)
		self._consecutive_failures = 0
		return self._current


_DEFAULT_MIC_RESOLVERS: dict[tuple[str, str], MicResolver] = {}


def get_default_mic_resolver(mic_input: str, backend: str) -> MicResolver:
	key = ((mic_input or "").strip(), (backend or "auto").strip().lower())
	resolver = _DEFAULT_MIC_RESOLVERS.get(key)
	if resolver is not None:
		return resolver

	priority = os.getenv("VOICE_MIC_PRIORITY", "").strip()
	if priority:
		usb_backend = os.getenv("VOICE_MIC_USB_BACKEND", "alsa")
		usb_input = os.getenv("VOICE_MIC_USB_INPUT", mic_input)
		onboard_backend = os.getenv("VOICE_MIC_ONBOARD_BACKEND", "pulse")
		onboard_input = os.getenv("VOICE_MIC_ONBOARD_INPUT", "regular0")
		options = build_mic_options(priority, usb_backend, usb_input, onboard_backend, onboard_input)
	else:
		options = build_legacy_mic_options(mic_input, backend)

	resolver = MicResolver(
		options=options,
		min_level_dbfs=_env_optional_float("VOICE_MIC_MIN_LEVEL_DBFS"),
		strict=_env_bool("VOICE_MIC_STRICT", False),
		recheck_sec=float(os.getenv("VOICE_MIC_RECHECK_SEC", "60")),
	)
	_DEFAULT_MIC_RESOLVERS[key] = resolver
	return resolver


def record_audio_auto_backend(
	out_wav: Path,
	duration: float,
	mic_input: str,
	backend: str,
	resolver: MicResolver | None = None,
) -> MicOption:
	return (resolver or get_default_mic_resolver(mic_input, backend)).record(out_wav, duration)


def play_wav(wav_path: Path) -> None:
	p = run_cmd([
		"ffplay",
		"-nodisp",
		"-autoexit",
		"-loglevel",
		"error",
		str(wav_path),
	])
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffplay failed")


def convert_to_wav_16k_mono(src: Path, dst: Path) -> None:
	p = run_cmd(
		[
			"ffmpeg",
			"-y",
			"-i",
			str(src),
			"-ac",
			"1",
			"-ar",
			"16000",
			str(dst),
			"-loglevel",
			"error",
		]
	)
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffmpeg convert failed")


def write_wav_mono_16k(path: Path, samples: np.ndarray) -> None:
	"""Write float32 audio samples as 16kHz mono WAV.

	This helper is used by speech capture logic when concatenating chunk files into a
	final recording. Keeping it separate avoids repeating the WAV-writing boilerplate
	while preserving the same 16-bit PCM format the ASR pipeline expects.
	"""
	import wave

	samples = np.asarray(samples, dtype=np.float32)
	samples = np.clip(samples, -1.0, 1.0)
	pcm16 = (samples * 32767.0).astype(np.int16)
	with wave.open(str(path), "wb") as wf:
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(16000)
		wf.writeframes(pcm16.tobytes())


def load_wav_mono_16k_float(path: Path):
	import wave

	with wave.open(str(path), "rb") as wf:
		channels = wf.getnchannels()
		sample_width = wf.getsampwidth()
		sample_rate = wf.getframerate()
		frames = wf.readframes(wf.getnframes())

	if sample_width != 2:
		raise RuntimeError(f"Expected 16-bit PCM wav, got sample_width={sample_width}")

	data = np.frombuffer(frames, dtype=np.int16)
	if channels > 1:
		data = data[::channels]
	samples = data.astype(np.float32) / 32768.0
	return sample_rate, samples


def dbfs_from_samples(samples: np.ndarray) -> float:
	if samples.size == 0:
		return -99.0
	rms = float(np.sqrt(np.mean(np.square(samples))))
	if rms <= 1e-9:
		return -99.0
	return 20.0 * math.log10(rms)


def wav_level_dbfs(path: Path) -> float:
	_, samples = load_wav_mono_16k_float(path)
	return dbfs_from_samples(samples)


def _runtime_float_env(primary_name: str, default: float, *aliases: str) -> float:
	for name in (primary_name, *aliases):
		value = os.getenv(name)
		if value is None:
			continue
		try:
			return float(value)
		except ValueError:
			continue
	return float(default)


def normalize_audio_gain(
	samples: np.ndarray,
	target_rms_dbfs: float | None = None,
	max_gain_db: float | None = None,
	peak_ceiling_dbfs: float | None = None,
) -> tuple[np.ndarray, float]:
	"""把低电平录音提升到适合 ASR 的响度，返回 (新采样, 实际增益dB)。

	部分 USB 麦克风即使把硬件采集增益拉到 100%，近距离说话也只有
	-65~-70 dBFS，直接送 ASR 会严重误识。这里按 RMS 做数字增益补偿，
	同时用峰值预留 headroom，避免削波。只做提升、不做衰减。
	"""
	if target_rms_dbfs is None:
		target_rms_dbfs = _runtime_float_env(
			"VOICE_ASR_GAIN_TARGET_DBFS",
			-20.0,
			"VOICE_ASR_TARGET_RMS_DBFS",
		)
	if max_gain_db is None:
		max_gain_db = _runtime_float_env("VOICE_ASR_MAX_GAIN_DB", 40.0)
	if peak_ceiling_dbfs is None:
		peak_ceiling_dbfs = _runtime_float_env("VOICE_ASR_PEAK_CEILING_DBFS", -1.0)

	if samples.size == 0:
		return samples, 0.0

	rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
	if rms <= 1e-9:
		return samples, 0.0

	peak = float(np.max(np.abs(samples)))
	rms_db = 20.0 * math.log10(rms)
	peak_db = 20.0 * math.log10(max(peak, 1e-9))

	gain_db = target_rms_dbfs - rms_db
	gain_db = min(gain_db, peak_ceiling_dbfs - peak_db)
	gain_db = max(0.0, min(gain_db, max_gain_db))
	if gain_db <= 0.05:
		return samples, 0.0

	boosted = samples.astype(np.float32) * float(10.0 ** (gain_db / 20.0))
	boosted = np.clip(boosted, -1.0, 1.0)
	return boosted, gain_db


def record_speech_until_silence(
	out_wav: Path,
	mic_input: str,
	backend: str,
	min_duration: float,
	max_duration: float,
	tail_window_sec: float,
	silence_threshold_dbfs: float,
	chunk_duration: float = 1.2,
	consecutive_silence_chunks: int = 4,
	resolver: MicResolver | None = None,
) -> None:
	all_chunks = []
	total_sec = 0.0
	silence_count = 0
	tmp_files = []
	active_resolver = resolver or get_default_mic_resolver(mic_input, backend)
	adaptive_base_threshold = float(silence_threshold_dbfs)
	dynamic_threshold_dbfs = adaptive_base_threshold
	speech_peak_dbfs = -99.0

	def _record_chunk(this_dur: float, label: str):
		nonlocal total_sec, speech_peak_dbfs, dynamic_threshold_dbfs
		chunk_wav = out_wav.parent / f"{out_wav.stem}.chunk.{len(tmp_files)}.wav"
		tmp_files.append(chunk_wav)

		# ffmpeg 偶发返回 0 但未落盘（PulseAudio 源不稳定时），重试最多 3 次
		recorded = False
		for attempt in range(3):
			record_audio_auto_backend(
				chunk_wav,
				duration=this_dur,
				mic_input=mic_input,
				backend=backend,
				resolver=active_resolver,
			)
			if chunk_wav.exists() and chunk_wav.stat().st_size > 44:  # WAV 头至少 44 字节
				recorded = True
				break
			print(f"[MIC] chunk recording produced no file, retry {attempt + 1}/3")
			chunk_wav.unlink(missing_ok=True)
		if not recorded:
			raise RuntimeError(
				f"Failed to record audio chunk after retries: {chunk_wav} "
				"(mic busy or PulseAudio unstable)"
			)

		try:
			sr, chunk_samples = load_wav_mono_16k_float(chunk_wav)
		except FileNotFoundError:
			raise RuntimeError(f"Recorded chunk vanished: {chunk_wav}")

		all_chunks.append(chunk_samples)
		total_sec += (len(chunk_samples) / float(sr))

		tail_n = max(1, int(sr * tail_window_sec))
		tail = chunk_samples[-tail_n:] if len(chunk_samples) >= tail_n else chunk_samples
		tail_db = dbfs_from_samples(tail)

		speech_peak_dbfs = max(speech_peak_dbfs, tail_db)
		if speech_peak_dbfs > -98.0:
			# 自适应阈值：用已观测到的语音峰值反推静音线。
			# 语音峰值越高，静音线可适当上抬，减少高噪环境下的“只到 max_duration 才停”。
			adaptive_from_peak = min(-34.0, speech_peak_dbfs - 10.0)
			dynamic_threshold_dbfs = max(adaptive_base_threshold, adaptive_from_peak)

		print(
			f"[MIC] {label} tail level: {tail_db:.1f} dBFS "
			f"(captured {total_sec:.1f}s, silence<{dynamic_threshold_dbfs:.1f}dBFS)"
		)
		return tail_db

	while total_sec < max_duration:
		remain = max_duration - total_sec
		if remain < MIN_RECORDABLE_CHUNK_SEC:
			break
		this_dur = min(chunk_duration, remain)
		tail_db = _record_chunk(this_dur, "speech")

		if total_sec >= min_duration:
			if tail_db < dynamic_threshold_dbfs:
				silence_count += 1
			else:
				silence_count = 0

			if silence_count >= consecutive_silence_chunks:
				remain = max_duration - total_sec
				# 剩余时长不足以录出可校验的分片时直接收尾（确认帧同理）。
				if remain < MIN_RECORDABLE_CHUNK_SEC:
					break

				# 确认帧：避免句中长停顿被误判结束
				confirm_dur = min(chunk_duration, remain)
				confirm_tail_db = _record_chunk(confirm_dur, "confirm")
				if confirm_tail_db < dynamic_threshold_dbfs:
					print("[MIC] silence confirmed, stop capture.")
					break
				print("[MIC] confirm frame has speech, continue listening.")
				silence_count = 0

	if not all_chunks:
		write_wav_mono_16k(out_wav, np.zeros((0,), dtype=np.float32))
	else:
		merged = np.concatenate(all_chunks, axis=0)
		write_wav_mono_16k(out_wav, merged)

	for f in tmp_files:
		f.unlink(missing_ok=True)


def _download_model_archive(url: str, dest_dir: Path, archive_name: str) -> Path:
	"""下载并解压 tar.bz2 模型包，返回解压目录。"""
	dest_dir.mkdir(parents=True, exist_ok=True)
	archive = dest_dir / archive_name
	print(f"[ASR] downloading {archive_name} ...")
	p = run_cmd(["curl", "-L", "-o", str(archive), url])
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"Failed to download {url}")

	extract_dir = dest_dir / "_extract"
	if extract_dir.exists():
		shutil.rmtree(extract_dir)
	extract_dir.mkdir(parents=True, exist_ok=True)
	with tarfile.open(archive, "r:bz2") as tf:
		tf.extractall(extract_dir)
	archive.unlink(missing_ok=True)
	return extract_dir


def _model_dir_with_files(eng_dir: Path):
	"""在引擎目录（或其直接子目录）中查找含完整模型文件的目录。

	兼容两种布局：
	  - 扁平：model_conformer/encoder-*.onnx ...
	  - 嵌套：model_conformer/sherpa-onnx-conformer-zh-.../encoder-*.onnx ...
	返回第一个匹配的目录；找不到返回 None。
	"""
	if not eng_dir.exists():
		return None
	candidates = [eng_dir]
	candidates += sorted(d for d in eng_dir.iterdir() if d.is_dir())
	for base in candidates:
		if (
			list(base.glob("*encoder*.onnx"))
			and list(base.glob("*decoder*.onnx"))
			and list(base.glob("*joiner*.onnx"))
			and (base / "tokens.txt").exists()
		):
			return base
	return None


def ensure_transducer_model(asr_root: Path, engine: str, force_download: bool = False) -> dict:
	"""获取支持热词的 transducer 模型，返回 {engine, encoder, decoder, joiner, tokens, root}。

	默认不预下载 Conformer，避免安装阶段把大模型一并拉下来；只有在显式开启
	force_download 或用户明确选用 conformer 并开启下载开关时才下载。
	"""
	if engine == "conformer":
		url = CONFORMER_MODEL_URL
		rel_dir = "sherpa-onnx-conformer-zh-stateless2-2023-05-23"
		eng_dir = asr_root / "model_conformer"
	else:
		raise ValueError(f"unsupported transducer engine: {engine}")

	base = _model_dir_with_files(eng_dir)
	if base is None and force_download:
		# 仅当目录里确实没有完整模型时才下载解压
		extract_dir = _download_model_archive(url, asr_root, rel_dir + ".tar.bz2")
		src = _model_dir_with_files(extract_dir)
		if src is None:
			shutil.rmtree(extract_dir, ignore_errors=True)
			raise RuntimeError(f"模型包解压后未找到模型文件: {extract_dir}")
		eng_dir.mkdir(parents=True, exist_ok=True)
		# 把解压出的文件并入 eng_dir，绝不 rmtree 已有目录
		for item in src.iterdir():
			dst = eng_dir / item.name
			if dst.exists():
				if dst.is_dir():
					shutil.rmtree(dst)
				else:
					dst.unlink()
			shutil.move(str(item), str(dst))
		shutil.rmtree(extract_dir, ignore_errors=True)
		print(f"[ASR] {engine} model ready: {eng_dir}")
		base = eng_dir

	if base is None:
		raise RuntimeError(
			f"{engine} 模型未找到: {eng_dir}。"
			"默认不自动下载 Conformer；如需使用，请显式开启 --download-conformer-model "
			"或调用 with force_download=True。"
		)

	def _pick(part: str) -> str:
		cands = sorted(base.glob(f"*{part}*.onnx"))
		if not cands:
			raise RuntimeError(f"missing {part} onnx under {base}")
		if part == "encoder":
			int8 = [c for c in cands if "int8" in c.name]
			return str((int8 or cands)[0])
		non_int8 = [c for c in cands if "int8" not in c.name]
		return str((non_int8 or cands)[0])

	return {
		"engine": engine,
		"encoder": _pick("encoder"),
		"decoder": _pick("decoder"),
		"joiner": _pick("joiner"),
		"tokens": str(base / "tokens.txt"),
		"root": str(base),
	}


def ensure_sensevoice_model(asr_model_dir: Path, force_download: bool) -> Path:
	def _pick_model(base: Path) -> Path | None:
		int8_model = base / "model.int8.onnx"
		fp_model = base / "model.onnx"
		tokens = base / "tokens.txt"
		if tokens.exists() and int8_model.exists():
			return int8_model
		if tokens.exists() and fp_model.exists():
			return fp_model
		return None

	def _promote_to_target(src: Path, dst: Path) -> Path:
		dst.mkdir(parents=True, exist_ok=True)
		for name in ("tokens.txt", "model.int8.onnx", "model.onnx"):
			s = src / name
			d = dst / name
			if not s.exists():
				continue
			if d.exists():
				d.unlink()
			shutil.move(str(s), str(d))

		# 可选 ITN 资源，存在则一并带上。
		for name in ("dict", "lexicon.txt", "replace.fst"):
			s = src / name
			d = dst / name
			if not s.exists():
				continue
			if d.exists():
				if d.is_dir():
					shutil.rmtree(d)
				else:
					d.unlink()
			shutil.move(str(s), str(d))

		picked = _pick_model(dst)
		if picked is None:
			raise RuntimeError(f"SenseVoice files incomplete under {dst}")
		return picked

	asr_model_dir.mkdir(parents=True, exist_ok=True)

	# 1) 标准目标目录已就绪
	picked = _pick_model(asr_model_dir)
	if picked is not None:
		return picked

	# 2) 兼容历史嵌套目录：把文件提升到 asr_root/model/
	for child in sorted(p for p in asr_model_dir.iterdir() if p.is_dir()):
		picked = _pick_model(child)
		if picked is not None:
			return _promote_to_target(child, asr_model_dir)

	if not force_download:
		raise RuntimeError(
			f"SenseVoice model not found under {asr_model_dir}. "
			"Place model.int8.onnx/model.onnx + tokens.txt, or enable auto download."
		)

	# 3) 自动下载并解压，兼容一层嵌套目录
	extract_dir = _download_model_archive(
		SENSEVOICE_MODEL_URL,
		asr_model_dir.parent,
		SENSEVOICE_MODEL_DIRNAME + ".tar.bz2",
	)
	try:
		candidates = [extract_dir, extract_dir / SENSEVOICE_MODEL_DIRNAME]
		candidates += sorted(p for p in extract_dir.iterdir() if p.is_dir())
		src_dir = next((c for c in candidates if _pick_model(c) is not None), None)
		if src_dir is None:
			raise RuntimeError(f"SenseVoice archive missing required files: {extract_dir}")
		picked = _promote_to_target(src_dir, asr_model_dir)
		print(f"[ASR] sensevoice model ready: {asr_model_dir}")
		return picked
	finally:
		shutil.rmtree(extract_dir, ignore_errors=True)


def build_asr_recognizer(
	asr_root: Path,
	model_path: Path,
	language: str,
	engine: str = "sensevoice",
	hotwords_file: str = "",
	hotwords_score: float = 2.5,
	force_download: bool = False,
):
	threads = max(1, (os.cpu_count() or 2) // 2)

	# 热词路径：conformer（离线 transducer）支持热词
	if engine == "conformer":
		files = ensure_transducer_model(asr_root, engine, force_download=force_download)
		hw = hotwords_for_cjkchar(
			ensure_hotwords_file(Path(hotwords_file) if hotwords_file else None)
		)
		common = {
			"tokens": files["tokens"],
			"encoder": files["encoder"],
			"decoder": files["decoder"],
			"joiner": files["joiner"],
			"num_threads": threads,
			"decoding_method": "modified_beam_search",
			"hotwords_file": str(hw),
			"hotwords_score": hotwords_score,
			"modeling_unit": "cjkchar",
			"provider": "cpu",
		}
		return sherpa_onnx.OfflineRecognizer.from_transducer(**common)

	# 默认 SenseVoice（离线，不支持热词）
	tokens = asr_root / "model" / "tokens.txt"
	if not tokens.exists():
		raise RuntimeError(f"Missing ASR tokens file: {tokens}")

	hr_dict_dir = asr_root / "model" / "dict"
	hr_lexicon = asr_root / "model" / "lexicon.txt"
	hr_rule_fsts = asr_root / "model" / "replace.fst"
	use_hr = hr_dict_dir.exists() and hr_lexicon.exists() and hr_rule_fsts.exists()

	kwargs = {
		"model": str(model_path),
		"tokens": str(tokens),
		"use_itn": True,
		"language": language,
		"num_threads": threads,
		"provider": "cpu",
		"debug": False,
	}
	if use_hr:
		kwargs.update(
			{
				"hr_dict_dir": str(hr_dict_dir),
				"hr_lexicon": str(hr_lexicon),
				"hr_rule_fsts": str(hr_rule_fsts),
			}
		)

	return sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)


def asr_transcribe(recognizer, wav_path: Path, engine: str = "sensevoice") -> str:
	sample_rate, samples = load_wav_mono_16k_float(wav_path)

	# 麦克风原始电平可能极低（实测近距离仅 -65~-70 dBFS），先做增益补偿再识别，
	# 否则唤醒词与指令都会被严重误识。
	samples, _gain_db = normalize_audio_gain(samples)

	stream = recognizer.create_stream()
	stream.accept_waveform(sample_rate, samples)
	recognizer.decode_stream(stream)

	result = stream.result
	text = getattr(result, "text", "")
	if not text:
		text = str(result)
	return text.strip()


def _tts_assets_ready(model_dir: Path, vocoder_path: Path) -> bool:
	required = [
		model_dir / "model-steps-3.onnx",
		model_dir / "lexicon.txt",
		model_dir / "tokens.txt",
		model_dir / "phone-zh.fst",
		model_dir / "date-zh.fst",
		model_dir / "number-zh.fst",
		model_dir / "espeak-ng-data",
		vocoder_path,
	]
	return all(p.exists() for p in required)


def ensure_official_matcha_tts(tts_root: Path, force_download: bool = True):
	model_dir = tts_root / OFFICIAL_MATCHA_DIRNAME
	vocoder_path = tts_root / "vocos-16khz-univ.onnx"

	if _tts_assets_ready(model_dir, vocoder_path):
		return model_dir, vocoder_path

	if not force_download:
		raise RuntimeError(f"official TTS assets missing under {tts_root}")

	tts_root.mkdir(parents=True, exist_ok=True)
	archive = tts_root / f"{OFFICIAL_MATCHA_DIRNAME}.tar.bz2"
	print(f"[TTS] downloading official Matcha model: {archive.name}")
	p = run_cmd(["curl", "-L", "-o", str(archive), MATCHA_ZH_EN_TTS_URL])
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"Failed to download {MATCHA_ZH_EN_TTS_URL}")

	try:
		with tarfile.open(archive, "r:bz2") as tf:
			tf.extractall(tts_root)
	finally:
		archive.unlink(missing_ok=True)

	if not vocoder_path.exists():
		print("[TTS] downloading official vocoder: vocos-16khz-univ.onnx")
		p = run_cmd(["curl", "-L", "-o", str(vocoder_path), VOCODER_16KHZ_URL])
		if p.returncode != 0:
			raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"Failed to download {VOCODER_16KHZ_URL}")

	if not _tts_assets_ready(model_dir, vocoder_path):
		raise RuntimeError(f"official Matcha TTS assets are incomplete under {tts_root}")

	print(f"[TTS] official model ready: {model_dir}")
	return model_dir, vocoder_path


def build_tts(tts_root: Path):
	model_dir, vocoder_path = ensure_official_matcha_tts(tts_root, force_download=True)

	matcha = sherpa_onnx.OfflineTtsMatchaModelConfig(
		acoustic_model=str(model_dir / "model-steps-3.onnx"),
		vocoder=str(vocoder_path),
		lexicon=str(model_dir / "lexicon.txt"),
		tokens=str(model_dir / "tokens.txt"),
		data_dir=str(model_dir / "espeak-ng-data"),
	)
	model_cfg = sherpa_onnx.OfflineTtsModelConfig(
		matcha=matcha,
		num_threads=4,  # 本机实测: 4线程455ms最优；8线程1282ms反而更慢(线程调度/缓存竞争)
		provider="cpu",
	)

	rule_fsts = ",".join(
		[
			str(model_dir / "phone-zh.fst"),
			str(model_dir / "date-zh.fst"),
			str(model_dir / "number-zh.fst"),
		]
	)
	cfg = sherpa_onnx.OfflineTtsConfig(
		model=model_cfg,
		rule_fsts=rule_fsts,
		max_num_sentences=1,
	)

	if not cfg.validate():
		print("[TTS] warning: config validation failed, continue anyway")

	return sherpa_onnx.OfflineTts(cfg)


def tts_speak(tts, text: str, out_wav: Path, play: bool) -> None:
	audio = tts.generate(text, sid=0, speed=1.0)
	sherpa_onnx.write_wave(str(out_wav), audio.samples, audio.sample_rate)
	if play:
		play_wav(out_wav)


def make_reply(text: str):
	text = text.strip()
	if not text:
		return "我没有听清，请再说一遍。", False

	if any(k in text for k in ("退出", "结束", "停止对话", "拜拜", "再见")):
		return "好的，我先退下了。", True

	if "几点" in text or "时间" in text:
		now = dt.datetime.now().strftime("%H点%M分")
		return f"现在是{now}。", False

	if "你好" in text:
		return "你好，我在。", False

	return f"我听到你说：{text}", False


def parse_args():
	asr_root = _resolve_sdk_root("asr")
	tts_root = _resolve_sdk_root("tts")

	parser = argparse.ArgumentParser(description="Local voice chain: open-source ASR wake -> ASR -> TTS")
	parser.add_argument("--asr-root", default=str(asr_root))
	parser.add_argument("--tts-root", default=str(tts_root))
	parser.add_argument(
		"--wake-words",
		default=os.getenv("VOICE_WAKE_WORDS", "小远同学,xiaoyuan"),
		help="Comma-separated wake words detected via ASR text matching",
	)
	parser.add_argument("--wake-duration", type=float, default=3.0)
	parser.add_argument("--speech-duration", type=float, default=12.0, help="Max speech capture duration in seconds")
	parser.add_argument("--speech-min-duration", type=float, default=1.5, help="Minimum speech capture duration before silence can end turn")
	parser.add_argument("--speech-tail-window", type=float, default=0.8, help="Tail window size in seconds for silence detection")
	parser.add_argument("--speech-silence-threshold-dbfs", type=float, default=-42.0, help="Tail dBFS below this is treated as silence")
	parser.add_argument("--record-backend", choices=["auto", "pulse", "alsa"], default="auto")
	parser.add_argument("--mic-input", default="default")
	parser.add_argument("--wake-audio-file", default="", help="Use existing audio file for wakeup detection")
	parser.add_argument("--speech-audio-file", default="", help="Use existing audio file for ASR input")
	parser.add_argument(
		"--require-wake-each-turn",
		action="store_true",
		help="Require wake word before every dialog turn",
	)
	parser.add_argument(
		"--session-idle-rounds",
		type=int,
		default=3,
		help="After N consecutive empty ASR rounds, session sleeps and needs wake word again",
	)

	parser.add_argument("--asr-language", default="zh")
	parser.add_argument("--asr-model", default="")
	parser.add_argument("--no-auto-download-asr", action="store_true")
	parser.add_argument(
		"--asr-engine",
		choices=["sensevoice", "conformer"],
		default="sensevoice",
		help="sensevoice=ASR 文本唤醒(默认)；conformer=离线ASR+热词支持",
	)
	parser.add_argument(
		"--download-conformer-model",
		action="store_true",
		default=False,
		help="默认不预下载 Conformer；显式启用时才下载并准备热词模型",
	)
	parser.add_argument("--hotwords-file", default="", help="热词文件路径，默认自动生成 hotwords.txt")
	parser.add_argument("--hotwords-score", type=float, default=3.0, help="热词增益分数；唤醒词已加入热词表，3.0 左右均衡")

	parser.add_argument("--once", action="store_true", help="Run one dialog turn and exit")
	parser.add_argument("--no-play", action="store_true", help="Do not play TTS audio")
	parser.add_argument("--work-dir", default="/tmp/local_voice_chat")
	return parser.parse_args()


def main():
	args = parse_args()

	check_cmd_exists("ffmpeg")
	check_cmd_exists("ffplay")
	check_cmd_exists("curl")

	asr_root = Path(args.asr_root)
	tts_root = Path(args.tts_root)
	wake_words = _parse_wake_words(args.wake_words)
	if not wake_words:
		raise RuntimeError("No wake words configured; set --wake-words or VOICE_WAKE_WORDS")

	work_dir = Path(args.work_dir)
	work_dir.mkdir(parents=True, exist_ok=True)

	if args.session_idle_rounds < 1:
		args.session_idle_rounds = 1

	if args.speech_min_duration < 0.5:
		args.speech_min_duration = 0.5
	if args.speech_duration < args.speech_min_duration:
		args.speech_duration = args.speech_min_duration

	if args.asr_engine == "sensevoice":
		asr_model_path = Path(args.asr_model) if args.asr_model else ensure_sensevoice_model(
			asr_root / "model",
			force_download=not args.no_auto_download_asr,
		)
	else:  # conformer
		asr_model_path = Path(args.asr_model) if args.asr_model else None

	print("[INIT] building ASR recognizer...")
	recognizer = build_asr_recognizer(
		asr_root,
		asr_model_path,
		args.asr_language,
		engine=args.asr_engine,
		hotwords_file=args.hotwords_file,
		hotwords_score=args.hotwords_score,
		force_download=(args.asr_engine == "conformer" and args.download_conformer_model),
	)
	print("[INIT] building TTS engine...")
	tts = build_tts(tts_root)
	print("[INIT] wake mode: open-source ASR text matching")
	print(f"[INIT] wake words: {', '.join(wake_words)}")
	if args.require_wake_each_turn:
		print("[INIT] dialog mode: require wake word for every turn")
	else:
		print("[INIT] dialog mode: wake once, then continuous conversation")
	print("[INIT] ready.")

	session_awake = False
	idle_rounds = 0
	file_mode = bool(args.wake_audio_file or args.speech_audio_file)

	while True:
		wake_wav = work_dir / "wake.wav"
		user_wav = work_dir / "user.wav"
		reply_wav = work_dir / "reply.wav"

		need_wake = args.require_wake_each_turn or (not session_awake)
		if need_wake:
			if args.wake_audio_file:
				wake_input = Path(args.wake_audio_file)
				if not wake_input.exists():
					raise RuntimeError(f"wake audio file not found: {wake_input}")
				wake_wav = wake_input
				print(f"\n[WAIT] using wake audio file: {wake_wav}")
			else:
				print(f"\n[WAIT] say wake word: {', '.join(wake_words)}")
				record_audio_auto_backend(
					wake_wav,
					duration=args.wake_duration,
					mic_input=args.mic_input,
					backend=args.record_backend,
				)
				level = wav_level_dbfs(wake_wav)
				print(f"[MIC] wake clip level: {level:.1f} dBFS")
				if level < -45:
					print("[MIC] warning: volume is very low, please move closer or increase mic gain")

			hit, hit_keyword, _raw = _detect_wakeup_open_asr(recognizer, wake_wav, wake_words, args.asr_engine)
			if not hit:
				print("[WAKE] no wake word detected.")
				print(f"[WAKE] expected wake words: {', '.join(wake_words)}")
				if args.wake_audio_file:
					print("[EXIT] wake audio file mode finished")
					break
				continue

			print(f"[WAKE] wake word detected: {hit_keyword or '(unknown)'}")
			session_awake = True
			idle_rounds = 0
			if not args.require_wake_each_turn:
				print("[SESSION] wake accepted. Continuous dialog is active.")

		if args.speech_audio_file:
			speech_input = Path(args.speech_audio_file)
			if not speech_input.exists():
				raise RuntimeError(f"speech audio file not found: {speech_input}")
			print(f"[ASR] using speech audio file: {speech_input}")
			convert_to_wav_16k_mono(speech_input, user_wav)
		else:
			if args.require_wake_each_turn:
				print("[ASR] listening for user query...")
			else:
				print("[ASR] listening... (no wake word needed now)")
			print(
				f"[ASR] dynamic capture: min={args.speech_min_duration:.1f}s, "
				f"max={args.speech_duration:.1f}s, silence<{args.speech_silence_threshold_dbfs:.1f}dBFS"
			)
			record_speech_until_silence(
				user_wav,
				mic_input=args.mic_input,
				backend=args.record_backend,
				min_duration=args.speech_min_duration,
				max_duration=args.speech_duration,
				tail_window_sec=args.speech_tail_window,
				silence_threshold_dbfs=args.speech_silence_threshold_dbfs,
			)
			level = wav_level_dbfs(user_wav)
			print(f"[MIC] speech clip level: {level:.1f} dBFS")

		text = asr_transcribe(recognizer, user_wav, engine=args.asr_engine)
		print(f"[ASR] text: {text}")
		if not text:
			idle_rounds += 1
			print(f"[ASR] empty speech ({idle_rounds}/{args.session_idle_rounds})")
			if (not args.require_wake_each_turn) and idle_rounds >= args.session_idle_rounds:
				session_awake = False
				idle_rounds = 0
				print("[SESSION] idle timeout. Wake word is required again.")
			if file_mode:
				print("[EXIT] file mode finished")
				break
			continue

		idle_rounds = 0
		reply, should_exit = make_reply(text)
		print(f"[TTS] reply: {reply}")
		tts_speak(tts, reply, reply_wav, play=not args.no_play)

		if file_mode:
			print("[EXIT] file mode finished")
			break

		if args.once or should_exit:
			print("[EXIT] done")
			break


if __name__ == "__main__":
	main()
