#!/usr/bin/env python3
"""
MOSS-TTS-Nano 流式朗读工具

调用本地 app_onnx.py 服务的 HTTP 流式接口，边生成边播放。
服务需提前启动，在 chat.bat 中加入：
    D:\\SCE\\App\\Anaconda\\Scripts\\conda.exe run -n py312 python E:\\WorkSpace\\PySpace\\pythonProject\\Project\\MOSS-TTS-Nano\\app_onnx.py

两种使用方式：
    【直接运行】 python tts_speak.py  → 执行底部 __main__ 中的 tts_speak() 调用
    【编程调用】 from tts_speak import tts_speak; tts_speak(text="你好世界")
"""

import io
import json
import os
import re
import sys
import time
import wave
import urllib.request

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_AUDIO_DIR = os.path.join(TOOLS_DIR, "assets", "audio")
OUTPUT_DIR = os.path.join(TOOLS_DIR, "output")

# 服务地址
BASE_URL = "http://localhost:18083"
# 默认参考音频
DEFAULT_PROMPT_AUDIO = os.path.join(ASSETS_AUDIO_DIR, "zh_3.wav")


# ─── 工具函数 ───────────────────────────────────────────

def normalize_text(text: str) -> str:
    """
    规范化中文文本：
    1. 保留中文常用日常符号：，。......！？
    2. 其他日常符号（——、~等）转换为：，
    3. 删除不需要的符号（空格、|等）和英文
    """
    # 允许的中文字符范围和常用符号
    # 中文字符: \u4e00-\u9fff
    # 允许的符号: ，。......！？
    
    # 第一步：将其他常见的日常符号转换为中文逗号
    # 处理中文破折号、波浪号、英文双横线等
    text = re.sub(r'[——~～--\-—]+', '，', text)
    
    # 第二步：删除不需要的符号
    # 删除：空格、竖线、括号、方括号、引号等不常见的符号
    # 保留：中文字符、数字、以及常用中文标点
    # 模式：保留 中文字符 + 数字 + 常用标点(，。......！？)
    text = re.sub(r'[^\u4e00-\u9fff0-9，。……！？]', '', text)
    
    # 第三步：处理多个相连的逗号
    text = re.sub(r'，+', '，', text)
    
    return text


def resolve_prompt_audio_path(voice_name: str) -> str:
    """根据声音名解析对应的参考音频路径"""
    audio_map = {
        "Junhao": "zh_6.wav",
        "zh_1": "zh_1.wav",
        "zh_3": "zh_3.wav",
        "zh_4": "zh_4.wav",
        "zh_6": "zh_6.wav",
        "zh_10": "zh_10.wav",
        "zh_11": "zh_11.wav",
        "en_2": "en_2.wav",
        "en_3": "en_3.wav",
        "en_4": "en_4.wav",
        "en_6": "en_6.wav",
        "en_7": "en_7.wav",
        "en_8": "en_8.wav",
        "jp_2": "jp_2.wav",
    }
    matched = audio_map.get(voice_name)
    if matched:
        print(f"[TTS] 找到声音: {voice_name}")
        path = os.path.join(ASSETS_AUDIO_DIR, matched)
        if os.path.isfile(path):
            return path
    print(f"[TTS] 未找到声音: {voice_name}，使用默认")
    return DEFAULT_PROMPT_AUDIO


def check_service() -> bool:
    """检查服务是否在运行"""
    try:
        req = urllib.request.Request(f"{BASE_URL}/health")
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def _start_stream(text: str, voice: str = "zh_11", timeout: int = 120) -> dict:
    """启动流式合成任务，返回 stream_id 等信息"""
    prompt_audio = resolve_prompt_audio_path(voice)

    boundary = "----WebKitFormBoundary" + os.urandom(8).hex()
    body = io.BytesIO()
    writer = lambda s: body.write(s.encode("utf-8"))

    writer(f"--{boundary}\r\n")
    writer(f'Content-Disposition: form-data; name="text"\r\n\r\n')
    writer(f"{text}\r\n")

    writer(f"--{boundary}\r\n")
    writer(f'Content-Disposition: form-data; name="demo_id"\r\n\r\n')
    writer(f"\r\n")

    writer(f"--{boundary}\r\n")
    writer(f'Content-Disposition: form-data; name="voice_clone_max_text_tokens"\r\n\r\n')
    writer(f"500\r\n")

    writer(f"--{boundary}\r\n")
    writer(f'Content-Disposition: form-data; name="max_new_frames"\r\n\r\n')
    writer(f"6000\r\n")

    writer(f"--{boundary}\r\n")
    writer(f'Content-Disposition: form-data; name="cpu_threads"\r\n\r\n')
    writer(f"4\r\n")

    if os.path.isfile(prompt_audio):
        with open(prompt_audio, "rb") as f:
            audio_data = f.read()
        writer(f"--{boundary}\r\n")
        writer(f'Content-Disposition: form-data; name="prompt_audio"; filename="{os.path.basename(prompt_audio)}"\r\n')
        writer(f"Content-Type: audio/wav\r\n\r\n")
        body.write(audio_data)
        writer(f"\r\n")

    writer(f"--{boundary}--\r\n")

    content_type = f"multipart/form-data; boundary={boundary}"
    data = body.getvalue()

    req = urllib.request.Request(
        f"{BASE_URL}/api/generate-stream/start",
        data=data,
        headers={"Content-Type": content_type},
        method="POST",
    )

    resp = urllib.request.urlopen(req, timeout=timeout)
    result = json.loads(resp.read().decode("utf-8"))

    if "error" in result:
        raise RuntimeError(f"服务返回错误: {result['error']}")

    return result


def _stream_and_play(stream_id: str, sample_rate: int = 48000,
                     channels: int = 2, timeout: int = 300) -> bytes:
    """从流式音频接口读取 PCM 数据，边接收边播放，返回完整 PCM"""
    import pyaudio

    audio_url = f"{BASE_URL}/api/generate-stream/{stream_id}/audio"

    p = pyaudio.PyAudio()
    stream = p.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=sample_rate,
        output=True,
        frames_per_buffer=4096,
    )

    all_pcm = bytearray()
    chunk_count = 0

    req = urllib.request.Request(audio_url)
    resp = urllib.request.urlopen(req, timeout=timeout)

    resp_sample_rate = int(resp.headers.get("X-Audio-Sample-Rate", str(sample_rate)))
    resp_channels = int(resp.headers.get("X-Audio-Channels", str(channels)))
    if resp_sample_rate != sample_rate or resp_channels != channels:
        sample_rate = resp_sample_rate
        channels = resp_channels
        stream.stop_stream()
        stream.close()
        stream = p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=sample_rate,
            output=True,
            frames_per_buffer=4096,
        )

    print(f"[TTS] 开始流式播放 (采样率={sample_rate}, 声道={channels})")

    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        all_pcm.extend(chunk)
        chunk_count += 1
        stream.write(chunk)

    stream.stop_stream()
    stream.close()
    p.terminate()

    audio_seconds = len(all_pcm) / (sample_rate * channels * 2)
    print(f"[TTS] 播放完成: {chunk_count} 个块, {audio_seconds:.1f}s, {len(all_pcm)/1024:.0f}KB")

    return bytes(all_pcm)


def _fetch_no_play(stream_id: str, timeout: int = 300) -> bytes:
    """只拉取音频数据，不播放"""
    audio_url = f"{BASE_URL}/api/generate-stream/{stream_id}/audio"
    resp = urllib.request.urlopen(audio_url, timeout=timeout)
    all_pcm = bytearray()
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        all_pcm.extend(chunk)
    return bytes(all_pcm)


def save_wav(pcm_data: bytes, sample_rate: int = 48000, channels: int = 2,
             output_path: str = None) -> str:
    """将 PCM 数据保存为 WAV 文件"""
    if not output_path:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        ts = int(time.time() * 1000)
        output_path = os.path.join(OUTPUT_DIR, f"tts_output_{ts}.wav")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)

    with open(output_path, "wb") as f:
        f.write(buf.getvalue())

    return output_path


# ─── 核心接口函数（可直接 import 调用，也可直接运行）────

def tts_speak(
    text="请输入要朗读的文字",
    voice="zh_6",
    play=True,
    save=True,
    output=None,
):
    """
    调用 MOSS-TTS-Nano 流式合成并播放。

    参数:
        text   (str)  : 要朗读的文字
        voice  (str)  : 声音预设，默认 "zh_6" (Junhao)
                        可选: Junhao, zh_1, zh_3, zh_4, zh_6, zh_10, zh_11,
                              en_2, en_3, en_4, en_6, en_7, en_8, jp_2
        play   (bool) : 是否播放，默认 True
        save   (bool) : 是否保存 WAV 文件，默认 True
        output (str)  : 自定义 WAV 保存路径，默认自动生成到 output/ 目录

    返回:
        dict: {
            "stream_id":   str,
            "sample_rate": int,
            "channels":    int,
            "elapsed":     float,
            "pcm_data":    bytes,
            "wav_path":    str or None,
        }
    """
    if not text or not isinstance(text, str):
        raise ValueError("text 参数不能为空，必须是一个字符串")

    # 规范化文本中的特殊符号
    text = normalize_text(text)

    if not check_service():
        raise RuntimeError(
            "[TTS] 服务未运行！请先启动 MOSS-TTS-Nano 服务：\n"
            "    D:\\SCE\\App\\Anaconda\\Scripts\\conda.exe run -n py312 python app_onnx.py"
        )

    print(f"[TTS] 正在合成: {text[:60]}{'...' if len(text) > 60 else ''}")
    t0 = time.time()
    stream_info = _start_stream(text, voice)
    stream_id = stream_info["stream_id"]
    sample_rate = stream_info.get("sample_rate", 48000)
    channels = stream_info.get("channels", 2)
    elapsed_prep = time.time() - t0
    print(f"[TTS] 流任务已创建: {stream_id} ({elapsed_prep:.1f}s)")

    if play:
        pcm_data = _stream_and_play(stream_id, sample_rate, channels)
    else:
        pcm_data = _fetch_no_play(stream_id)

    elapsed = time.time() - t0

    wav_path = None
    # if save:
    #     wav_path = save_wav(pcm_data, sample_rate, channels, output)
    #     print(f"[TTS] 已保存: {wav_path}")

    return {
        "stream_id": stream_id,
        "sample_rate": sample_rate,
        "channels": channels,
        "elapsed": elapsed,
        "pcm_data": pcm_data,
        "wav_path": wav_path,
    }


# ─── 直接运行入口 ───────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MOSS-TTS-Nano 朗读工具")
    parser.add_argument("--text", type=str, default="已写入 ，以后搜东西全程  优先，只有  顶不了才用 。还有，之前那个小女孩的声音文...",
                        help="要朗读的文字")
    parser.add_argument("--voice", type=str, default="zh_6",
                        help="声音预设: zh_1, zh_3, zh_4, zh_6, zh_10, zh_11, Junhao, en_2, en_3, en_4, en_6, en_7, en_8, jp_2")
    parser.add_argument("--no-play", action="store_true",
                        help="不播放，仅合成")
    args = parser.parse_args()
    tts_speak(text=args.text, voice=args.voice, play=not args.no_play)
