"""TASK-04a 音质降级防破坏保护：should_block_downgrade 纯函数测试。

治理决策表：
- 文件在盘 → 降级阻止 / 升级放行 / 同级放行 / 链外档位（无法可靠比较）放行
- 文件丢失 → 任意音质放行（不受比较约束）

纯函数 + DB 查询在调用侧（_download_with_account），本文件只测判定逻辑
（接入点正确性由 reviewer 审查）。
"""

import task_manager
from core.providers.base import MusicProvider
from models import Song, db


def _add_song(file_path, quality, sid="10086", platform="netease", status="success"):
    """向内存库插入一条 Song 记录并提交，返回该对象。"""
    song = Song(
        id=sid,
        platform=platform,
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        duration_ms=0,
        quality=quality,
        file_path=file_path,
        file_size=1,
        playlist_id=None,
        source_name="",
        status=status,
        account_id=None,
    )
    db.session.add(song)
    db.session.commit()
    return song


# ----------------------------------------------------------------------
# 降级阻止 / 升级同级放行
# ----------------------------------------------------------------------
def test_block_downgrade_lossless_to_standard(webapp_app, tmp_path):
    """1. 已有 lossless 且文件在盘，本次 actual=standard（降级）→ 阻止，
    文案须同时含已有档位与本次档位。"""
    (app,) = webapp_app
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fake flac")
    song = _add_song(str(audio), quality="lossless")

    block, why = task_manager.should_block_downgrade(song, "standard")
    assert block is True
    assert "lossless" in why
    assert "standard" in why


def test_allow_upgrade_standard_to_lossless(webapp_app, tmp_path):
    """2. 已有 standard 且文件在盘，本次 actual=lossless（升级）→ 放行。"""
    (app,) = webapp_app
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake mp3")
    song = _add_song(str(audio), quality="standard")

    block, why = task_manager.should_block_downgrade(song, "lossless")
    assert block is False
    assert why == ""


def test_allow_same_level(webapp_app, tmp_path):
    """3. 已有 lossless，本次 actual=lossless（同级）→ 放行。"""
    (app,) = webapp_app
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fake flac")
    song = _add_song(str(audio), quality="lossless")

    block, _ = task_manager.should_block_downgrade(song, "lossless")
    assert block is False


# ----------------------------------------------------------------------
# 文件丢失：不比较，任意音质放行
# ----------------------------------------------------------------------
def test_allow_when_file_missing_even_if_downgrade(webapp_app, tmp_path):
    """4. 已有 lossless 记录但 file_path 指向不存在路径 → 放行
    （文件丢失不比较；即使本次档位更低也放行）。"""
    (app,) = webapp_app
    missing = tmp_path / "deleted.flac"  # 不创建该文件
    song = _add_song(str(missing), quality="lossless")

    block, _ = task_manager.should_block_downgrade(song, "standard")
    assert block is False


# ----------------------------------------------------------------------
# 链外档位：无法可靠比较，放行
# ----------------------------------------------------------------------
def test_allow_out_of_chain_actual_dolby(webapp_app, tmp_path):
    """5. 已有 lossless，本次 actual=dolby（不在 QUALITY_ORDER）→ 放行。"""
    (app,) = webapp_app
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fake flac")
    song = _add_song(str(audio), quality="lossless")

    block, _ = task_manager.should_block_downgrade(song, "dolby")
    assert block is False


def test_allow_out_of_chain_actual_sky(webapp_app, tmp_path):
    """6. 已有 hires，本次 actual=sky（不在 QUALITY_ORDER）→ 放行。"""
    (app,) = webapp_app
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fake flac")
    song = _add_song(str(audio), quality="hires")

    block, _ = task_manager.should_block_downgrade(song, "sky")
    assert block is False


# ----------------------------------------------------------------------
# 空档位 / 无记录：放行
# ----------------------------------------------------------------------
def test_allow_empty_quality_and_none_record(webapp_app, tmp_path):
    """7. 已有记录 quality 为空串 → 放行；existing_song=None（无记录）→ 放行。"""
    (app,) = webapp_app
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake mp3")
    song = _add_song(str(audio), quality="")

    assert task_manager.should_block_downgrade(song, "standard")[0] is False
    assert task_manager.should_block_downgrade(None, "standard") == (False, "")


# ----------------------------------------------------------------------
# 守护：QUALITY_ORDER 序本身不被改动
# ----------------------------------------------------------------------
def test_quality_order_sequence_guard():
    """8. 档位序守护：index 小 = 档高，严格为
    jymaster > hires > lossless > exhigh > higher > standard。"""
    assert MusicProvider.QUALITY_ORDER == [
        "jymaster", "hires", "lossless", "exhigh", "higher", "standard",
    ]
    order = MusicProvider.QUALITY_ORDER
    for higher, lower in zip(order, order[1:]):
        assert order.index(higher) < order.index(lower)
