"""core.audio_fingerprint 测试（v0.9.0.0 声音内容去重）

核心场景（fnOS 实例实测）：《千里之外》被「依然范特西」与「闪亮2006…」
两张专辑收录为不同 song_id，音频帧逐字节相同，但写入的专辑名/封面不同，
整文件 MD5 与文件大小都不同。指纹必须只看声音部分。
"""

import hashlib
import struct

from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TALB, TIT2, APIC

from core.audio_fingerprint import audio_fingerprint

FRAMES = bytes(range(256)) * 64   # 伪造音频帧区（16KB）


def _streaminfo(md5: bytes) -> bytes:
    # min/max blocksize(16+16) min/max framesize(24+24)
    # 采样率20 | 声道-1 3 | 位深-1 5 | 总采样数36  = 64 bit
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 1000
    return struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + md5


def make_flac(path, md5: bytes = b"\x11" * 16, frames: bytes = FRAMES):
    si = _streaminfo(md5)
    block = bytes([0x80 | 0]) + len(si).to_bytes(3, "big") + si   # 唯一且最后一个块
    path.write_bytes(b"fLaC" + block + frames)
    return path


def tag_flac(path, album: str, cover: bytes):
    f = FLAC(str(path))
    f["title"] = "千里之外"
    f["album"] = album
    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.data = cover
    f.add_picture(pic)
    f.save()


def test_flac_same_audio_different_tags_match(tmp_path):
    a = make_flac(tmp_path / "a.flac")
    b = make_flac(tmp_path / "b.flac")
    tag_flac(a, "依然范特西", b"\xff\xd8" + b"A" * 3000)
    tag_flac(b, "闪亮2006 Hit FM年度百首单曲票选", b"\xff\xd8" + b"B" * 900)
    # 前提：整文件 MD5 与大小都不同（旧做法会漏判）
    assert a.stat().st_size != b.stat().st_size
    assert hashlib.md5(a.read_bytes()).digest() != hashlib.md5(b.read_bytes()).digest()
    fa, fb = audio_fingerprint(a), audio_fingerprint(b)
    assert fa == fb == "flac:" + (b"\x11" * 16).hex()


def test_flac_different_audio_not_match(tmp_path):
    a = make_flac(tmp_path / "a.flac", md5=b"\x11" * 16)
    b = make_flac(tmp_path / "b.flac", md5=b"\x22" * 16)
    assert audio_fingerprint(a) != audio_fingerprint(b)


def test_flac_zero_streaminfo_md5_uses_frames(tmp_path):
    a = make_flac(tmp_path / "a.flac", md5=b"\x00" * 16)
    b = make_flac(tmp_path / "b.flac", md5=b"\x00" * 16)
    c = make_flac(tmp_path / "c.flac", md5=b"\x00" * 16, frames=FRAMES[::-1])
    tag_flac(a, "专辑一", b"x" * 100)
    tag_flac(b, "专辑二", b"y" * 2000)
    fa = audio_fingerprint(a)
    assert fa == audio_fingerprint(b) == "flacraw:" + hashlib.md5(FRAMES).hexdigest()
    assert audio_fingerprint(c) != fa


def test_flac_with_id3_prefix(tmp_path):
    """库内实测存在开头多一段 ID3v2 的 .flac：跳过前缀后照常取 STREAMINFO"""
    plain = make_flac(tmp_path / "plain.flac")
    raw = plain.read_bytes()
    id3_body = b"\x00" * 20
    prefix = b"ID3\x03\x00\x00" + bytes([0, 0, 0, len(id3_body)]) + id3_body
    pref = tmp_path / "pref.flac"
    pref.write_bytes(prefix + raw)
    assert audio_fingerprint(pref) == audio_fingerprint(plain)


MP3_FRAMES = b"\xff\xfb\x90\x64" + bytes(range(200)) * 50


def test_mp3_strips_id3v2_id3v1_ape(tmp_path):
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    a.write_bytes(MP3_FRAMES)
    b.write_bytes(MP3_FRAMES)
    ta = ID3()
    ta.add(TIT2(encoding=3, text="歌"))
    ta.add(TALB(encoding=3, text="专辑一"))
    ta.save(str(a))
    tb = ID3()
    tb.add(TALB(encoding=3, text="另一张专辑"))
    tb.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"c" * 1500))
    tb.save(str(b))
    # b 结尾再追加 APEv2（无头）与 ID3v1
    ape_items = b"x" * 40
    ape_footer = (b"APETAGEX" + struct.pack("<IIII", 2000, len(ape_items) + 32, 1, 0)
                  + b"\x00" * 8)
    with open(b, "ab") as fh:
        fh.write(ape_items + ape_footer + b"TAG" + b"\x00" * 125)
    fa = audio_fingerprint(a)
    assert fa == audio_fingerprint(b) == "mp3:" + hashlib.md5(MP3_FRAMES).hexdigest()


def test_fake_flac_extension_detected_as_mp3(tmp_path):
    """库内实测存在扩展名 .flac、内容是 MP3 试听片段的文件：按内容判断"""
    f = tmp_path / "fake.flac"
    f.write_bytes(MP3_FRAMES)
    assert audio_fingerprint(f).startswith("mp3:")


def test_empty_and_missing_return_none(tmp_path):
    e = tmp_path / "empty.flac"
    e.write_bytes(b"")
    assert audio_fingerprint(e) is None
    assert audio_fingerprint(tmp_path / "nope.flac") is None


def test_other_content_whole_file(tmp_path):
    f = tmp_path / "x.m4a"
    f.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"z" * 100)
    assert audio_fingerprint(f) == "raw:" + hashlib.md5(f.read_bytes()).hexdigest()
