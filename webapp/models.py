"""数据库模型 - SQLAlchemy + SQLite

表结构：
- Account：多平台账号管理（网易云/QQ音乐/酷狗音乐）
- Playlist：关注的歌单/榜单
- Song：已下载歌曲记录（去重依据）
- DownloadTask：下载任务（实时进度 + 失败列表）
- OrganizerJob：整理任务（音乐扫描/清理，独立于下载队列）
- LocalAudioFile：本地音频文件索引（最近一次成功扫描时的磁盘状态）
- Setting：配置项 key-value
"""

import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# 支持的音乐平台
PLATFORMS = ["netease", "qq", "kugou"]
PLATFORM_NAMES = {
    "netease": "网易云",
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
}


def vip_text_for(platform: str, vip_type: int) -> str:
    """会员类型文本（按平台区分）

    - netease: 0=非会员, 11=黑胶VIP, 12=SVIP
      （注意：SVIP 识别依赖 /vip/info 的 redplus 包，account.vipType 对
      SVIP 账号恒返 11，不可作为档位判据）
    - qq: 0=非会员, 1-8=绿钻VIP等级
    - kugou: 0=非会员, 1=VIP会员（client.get_user_info 按 is_vip 归一为 0/1）
    - 其他平台: 透传数字
    """
    if platform == "qq":
        return f"绿钻VIP Lv.{vip_type}" if vip_type > 0 else "非会员"
    if platform == "kugou":
        return "VIP会员" if vip_type > 0 else "非会员"
    return {0: "非会员", 11: "黑胶VIP", 12: "SVIP"}.get(vip_type, f"vipType={vip_type}")


class Account(db.Model):
    """多平台账号

    platform: netease(网易云) / qq(QQ音乐) / kugou(酷狗音乐)
    vip_type: 网易云 0=非会员/11=黑胶VIP/12=SVIP；QQ 0=非会员/1-8=绿钻等级
    quota_limit: 总额度，0=不限制
    vip_expire_at: 会员到期时间（来自 API，可能为空）
    sort_order: 使用顺序（升序），账号选择器按此排序（按平台独立）
    """
    __tablename__ = "accounts"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    platform = db.Column(db.String(20), default="netease", nullable=False)
    name = db.Column(db.String(100), nullable=False)      # 别名（如"主账号"）
    cookie = db.Column(db.Text, default="")
    nickname = db.Column(db.String(200), default="")      # 平台昵称
    vip_type = db.Column(db.Integer, default=0)
    vip_expire_at = db.Column(db.DateTime)                # 会员到期时间
    quota_limit = db.Column(db.Integer, default=0)        # 总额度，0=不限制
    sort_order = db.Column(db.Integer, default=0)         # 使用顺序（升序，平台内独立）
    enabled = db.Column(db.Boolean, default=True)
    last_check_at = db.Column(db.DateTime)                # 上次登录校验时间
    created_at = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self, monthly_downloaded: int | None = None) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "platform_name": PLATFORM_NAMES.get(self.platform, self.platform),
            "name": self.name,
            "nickname": self.nickname,
            "vip_type": self.vip_type,
            "vip_text": vip_text_for(self.platform, self.vip_type),
            "vip_expire_at": self.vip_expire_at.strftime("%Y-%m-%d %H:%M:%S") if self.vip_expire_at else None,
            "quota_limit": self.quota_limit,
            "sort_order": self.sort_order,
            "monthly_downloaded": monthly_downloaded if monthly_downloaded is not None else 0,
            "enabled": self.enabled,
            "last_check_at": self.last_check_at.strftime("%Y-%m-%d %H:%M:%S") if self.last_check_at else None,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None,
        }


class Playlist(db.Model):
    """关注的歌单/榜单

    type: official(官方榜单) / user(用户自定义歌单)
    enabled: 是否启用自动下载
    platform: 平台标识（netease / qq / kugou）
    """
    __tablename__ = "playlists"
    id = db.Column(db.Integer, primary_key=True)           # 平台歌单/榜单 ID
    platform = db.Column(db.String(20), default="netease", nullable=False)
    name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(20), default="official")    # official / user
    enabled = db.Column(db.Boolean, default=True)
    limit_count = db.Column(db.Integer, default=100)
    last_synced_at = db.Column(db.DateTime)
    track_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)

    songs = db.relationship("Song", backref="playlist", lazy="dynamic")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "platform_name": PLATFORM_NAMES.get(self.platform, self.platform),
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "limit_count": self.limit_count,
            "last_synced_at": self.last_synced_at.strftime("%Y-%m-%d %H:%M:%S") if self.last_synced_at else None,
            "track_count": self.track_count,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None,
        }


class Song(db.Model):
    """已下载歌曲记录（去重依据 + 下载历史）

    status: success / failed / skipped
    platform: 平台标识（netease / qq / kugou）
    id: 平台歌曲 ID（netease 为数字 ID 字符串，QQ 为 songmid 字符串）
    主键为 (id, platform) 复合主键：netease 数字 ID 与 QQ songmid 理论上可撞号，
    单列主键下会互相覆盖（N1 修复，旧库由 _migrate_song_pk_to_composite 重建迁移）
    """
    __tablename__ = "songs"
    id = db.Column(db.String(64), primary_key=True)        # 平台歌曲 ID（统一 str）
    platform = db.Column(db.String(20), primary_key=True, default="netease", nullable=False)
    name = db.Column(db.String(300), nullable=False)
    artists = db.Column(db.String(300), default="")
    album = db.Column(db.String(300), default="")
    duration_ms = db.Column(db.Integer, default=0)
    quality = db.Column(db.String(20), default="")
    file_path = db.Column(db.String(500), default="")
    file_size = db.Column(db.Integer, default=0)
    playlist_id = db.Column(db.Integer, db.ForeignKey("playlists.id"), nullable=True)
    downloaded_at = db.Column(db.DateTime, default=datetime.now)
    status = db.Column(db.String(20), default="success")   # success / failed / skipped
    error_msg = db.Column(db.String(500), default="")
    # failed 歌曲的来源歌单名（用于失败列表展示，避免 playlist_id 为空时丢失信息）
    source_name = db.Column(db.String(200), default="")
    # 记录用哪个账号下载的（用于本月下载额度统计）
    account_id = db.Column(db.Integer, nullable=True)


class DownloadTask(db.Model):
    """下载任务（实时进度跟踪）

    status: pending / downloading / paused / done / failed / skipped
        pending       已入队待处理
        downloading   正在处理
        paused        被用户暂停（可通过「继续」恢复；.part 断点保留）
        done / failed / skipped  终态
    fee: 网易云歌曲费用类型 0=免费 1=VIP 4=购买专辑 8=低音质免费
    platform: 平台标识（netease / qq / kugou）
    """
    __tablename__ = "download_tasks"
    pk = db.Column(db.Integer, primary_key=True, autoincrement=True)
    platform = db.Column(db.String(20), default="netease", nullable=False)
    song_id = db.Column(db.String(64), nullable=False)      # 平台歌曲 ID（统一 str）
    song_name = db.Column(db.String(300), default="")
    artists = db.Column(db.String(300), default="")
    playlist_id = db.Column(db.Integer, nullable=True)
    playlist_name = db.Column(db.String(200), default="")
    status = db.Column(db.String(20), default="pending")   # pending/downloading/paused/done/failed/skipped
    progress = db.Column(db.Integer, default=0)            # 0-100
    error_msg = db.Column(db.String(500), default="")
    account_id = db.Column(db.Integer, nullable=True)      # 本次下载用的账号
    fee = db.Column(db.Integer, default=0)                 # 歌曲费用类型，用于 VIP/非VIP 账号选择
    # 下载时刻的音质/文件快照（与 Song 同名字段同类型）：/api/songs 读取时
    # 优先用快照，空值回退 JOIN 的 Song（存量行兼容）。没有快照列时，同一首
    # 歌重新下载会覆盖唯一一条 Song，导致 N 条历史行全显示最新音质
    quality = db.Column(db.String(20), default="", nullable=False)
    file_path = db.Column(db.String(500), default="", nullable=False)
    file_size = db.Column(db.Integer, default=0, nullable=False)
    # 任务类型：normal=普通下载（含 force 重下）；upgrade=音质升级，取到的档位
    # 必须严格高于已有文件才下载（见 task_manager.should_skip_upgrade）
    kind = db.Column(db.String(20), default="normal", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self) -> dict:
        return {
            "pk": self.pk,
            "platform": self.platform,
            "platform_name": PLATFORM_NAMES.get(self.platform, self.platform),
            "song_id": self.song_id,
            "song_name": self.song_name,
            "artists": self.artists,
            "playlist_id": self.playlist_id,
            "playlist_name": self.playlist_name,
            "status": self.status,
            "kind": self.kind or "normal",
            "progress": self.progress,
            "error_msg": self.error_msg,
            "account_id": self.account_id,
            "fee": self.fee,
            "quality": self.quality,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None,
        }


# ======================================================================
# 下载任务状态集合
# ----------------------------------------------------------------------
# 历史上这两个集合的字面量散落在 task_manager / routes 的 8 处查询里，
# 新增状态（paused）时极易漏改，故统一收敛到此处单独定义：
#   - 漏改去重查询 → 同一首歌被重复入队下载
#   - 漏改 get_active_tasks → 任务从界面消失，用户无法继续/删除
# 新增在途状态时只改这里即可。
# ======================================================================

# 在途任务状态总集（含 paused）：去重查询、任务列表展示用。
# 语义：三者都表示"这首歌已被安排下载"，同一 (song_id, platform) 不应重复入队。
ACTIVE_TASK_STATUSES = ("pending", "downloading", "paused")

# 需要 worker 推进的状态（不含 paused）：启动恢复重建队列、判断"是否有活跃任务"用。
# paused 是用户显式状态，重启后刻意不重新入队，等用户点「继续」。
RUNNABLE_TASK_STATUSES = ("pending", "downloading")


class Setting(db.Model):
    """配置项 key-value 存储"""
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default="")

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        item = db.session.get(cls, key)
        return item.value if item else default

    @classmethod
    def set(cls, key: str, value: str) -> None:
        item = db.session.get(cls, key)
        if item:
            item.value = value
        else:
            item = cls(key=key, value=value)
            db.session.add(item)
        db.session.commit()

    @classmethod
    def get_all(cls) -> dict:
        return {item.key: item.value for item in cls.query.all()}


class OrganizerJob(db.Model):
    """整理任务（扫描/清理），独立于下载队列，单 Worker 串行执行

    status: pending / running / success / partial / failed / interrupted
        pending / running          在途（ORGANIZER_JOB_ACTIVE_STATUSES）
        success / partial / failed  终态
        interrupted                服务重启时被中断（不自动重试）
    progress: 0-100，无法度量时保持真实值不虚构
    result: JSON 摘要（成功时写入，见 OrganizerManager._execute_scan）
    """
    __tablename__ = "organizer_jobs"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    job_type = db.Column(db.String(20), nullable=False, default="scan")  # scan | clean（清理复用）
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    progress = db.Column(db.Integer, nullable=False, default=0)   # 0-100
    processed = db.Column(db.Integer, nullable=False, default=0)
    total = db.Column(db.Integer, nullable=False, default=0)
    error_msg = db.Column(db.Text, nullable=True)
    result = db.Column(db.Text, nullable=True)                    # JSON 摘要
    created_at = db.Column(db.DateTime, default=datetime.now)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)


# 在途整理任务状态：submit_scan 查重（拒绝并发第二个任务）、
# 启动恢复（pending/running → interrupted）共用，新增在途状态只改这里
ORGANIZER_JOB_ACTIVE_STATUSES = ("pending", "running")


class OrganizerCleanItem(db.Model):
    """一次清理任务的逐文件计划与结果（回收站落点可追溯）

    path: 待清理的原始绝对路径（提交时的计划，与 LocalAudioFile.path 同为
        绝对路径，但未强制 resolve 归一）
    keep_path: repair 保留路径，可空（无 repair 意图即为 NULL）
    status: pending | success | failed（逐文件执行前重验 + 执行结果）
    trash_dest: 实际落入 .trash 的路径，直删（organize_direct_delete）为 NULL
    finished_at: 该文件处理完成时刻（成功/失败均写，中断安全：每文件处理后
        立即 commit，已成功项的落盘事实先落库）
    """
    __tablename__ = "organizer_clean_items"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    job_id = db.Column(db.Integer, nullable=False, index=True)
    path = db.Column(db.String(500), nullable=False)          # 待清理的原始绝对路径
    keep_path = db.Column(db.String(500), nullable=True)      # repair 保留路径，可空
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending|success|failed
    error_msg = db.Column(db.String(500), nullable=True)
    trash_dest = db.Column(db.String(500), nullable=True)     # 实际落入 .trash 的路径，直删为 NULL
    finished_at = db.Column(db.DateTime, nullable=True)


class LocalAudioFile(db.Model):
    """本地音频文件索引：代表最近一次成功扫描时的磁盘状态，不是歌曲身份

    path: 绝对路径（output_dir 已 resolve，walk 产物直接 str()）
    status: active | missing | trashed | cleaned
        （trashed/cleaned 由清理任务使用；扫描只写 active/missing。
         missing 仅在 walk 完整成功后才允许标记，中途异常绝不触碰）
    content_hash: SHA1 十六进制，重复分组时惰性计算，文件变化时清空
    platform: 关联 songs 的平台标识（本阶段恒为 NULL，后续关联后回填）
    """
    __tablename__ = "local_audio_files"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    path = db.Column(db.String(500), nullable=False, unique=True, index=True)
    size = db.Column(db.Integer, nullable=False, default=0)
    mtime = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    ext = db.Column(db.String(10), nullable=False, default="")
    lossless = db.Column(db.Boolean, nullable=False, default=False)
    duration_ms = db.Column(db.Integer, nullable=False, default=0)
    bitrate_kbps = db.Column(db.Integer, nullable=False, default=0)
    sample_rate = db.Column(db.Integer, nullable=False, default=0)
    channels = db.Column(db.Integer, nullable=False, default=0)
    bits_per_sample = db.Column(db.Integer, nullable=False, default=0)
    title = db.Column(db.String(300), nullable=False, default="")
    artist = db.Column(db.String(300), nullable=False, default="")
    album = db.Column(db.String(300), nullable=False, default="")
    song_id = db.Column(db.String(64), nullable=True, index=True)   # 文件名解析的平台歌曲 ID，解析不出为 NULL
    platform = db.Column(db.String(20), nullable=True, index=True)
    content_hash = db.Column(db.String(64), nullable=True)
    last_scan_job_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class FileMd5Cache(db.Model):
    """音频声音指纹缓存（下载完成内容去重用，v0.9.0.0）

    键为文件绝对路径，(size, mtime) 是失效键：两者与磁盘现状一致才信任
    md5 列，否则视为过期需重算回写。md5 列存 core.audio_fingerprint 的
    结果（带格式前缀，如 "flac:<32位hex>"；读不出指纹的文件存空串）。覆盖下载目录全部文件（不限 songs 表登记
    的），DB 重置后文件仍在的场景也能命中重复。
    """
    __tablename__ = "file_md5_cache"
    path = db.Column(db.String(500), primary_key=True)
    size = db.Column(db.Integer, nullable=False, default=0)
    mtime = db.Column(db.Float, nullable=False, default=0.0)
    md5 = db.Column(db.String(64), nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


# 新装默认 Web 监听地址：仅本机
DEFAULT_WEB_PORT = "127.0.0.1:45600"


def default_web_port() -> str:
    """首次写库时使用的 web_port：环境变量 WEB_PORT_DEFAULT 优先（Docker 用）"""
    return (os.environ.get("WEB_PORT_DEFAULT") or "").strip() or DEFAULT_WEB_PORT


# 默认配置项
DEFAULT_SETTINGS = {
    # 网易云API服务相关
    "ncm_api_port": "45601",
    # 自定义API服务URL（勾选 use_custom_api_url 时生效）
    "use_custom_api_url": "false",
    "custom_api_url": "",
    # QQ音乐API服务相关（内置 qqmusic-api 二进制）
    "qq_api_port": "45602",
    # QQ音乐自定义API服务URL（勾选 use_custom_qq_api_url 时生效）
    "use_custom_qq_api_url": "false",
    "qq_api_base_url": "http://127.0.0.1:45602",
    # 酷狗音乐API服务相关（内置 kugou-api 二进制）
    "kugou_api_port": "45603",
    # 酷狗音乐自定义API服务URL（勾选 use_custom_kugou_api_url 时生效）
    "use_custom_kugou_api_url": "false",
    "kugou_api_base_url": "http://127.0.0.1:45603",
    # Web 服务监听地址（host:port，* 表示监听所有网卡）。
    # 新装默认只监听本机（无登录体系，监听全部网卡等于把账号 Cookie 暴露给
    # 整个局域网）；Docker 镜像经 WEB_PORT_DEFAULT 环境变量改为 0.0.0.0:45600。
    # 只影响首次写入，存量库已保存的值不变（见 init_db / default_web_port）
    "web_port": DEFAULT_WEB_PORT,
    "output_dir": "downloads",
    # 文件名包含专辑名（歌手 - 歌名 [专辑].ext）：同歌手同名不同版本可共存
    "filename_include_album": "true",
    # 同名文件已存在时覆盖重新下载（关闭时跳过下载并按成功处理）
    "overwrite_existing": "false",
    # 下载完成后按声音内容去重（只比声音部分，不比标签/封面）：与库内已有
    # 文件声音完全一致时回退本次下载
    # （删除新文件与新建空目录），任务标记 skipped 并复用库内文件
    "download_md5_dedup": "true",
    # 下载目录结构：artist = /歌手/歌曲；artist_album = /歌手/专辑 (年份)/歌曲
    # （专辑名缺失的单曲/EP 用歌名当专辑目录；(年份) 仅 artist_album 模式附加，
    # 用于区分同名不同版本的专辑/EP。切换只影响新下载；存量文件是否搬入
    # 新结构由用户在设置页手动触发，不做自动迁移）
    "dir_layout": "artist_album",
    # 音质档位按平台独立设置（空串=未单独设置，读取时回退旧全局 level 兼容迁移）
    # 档位值沿用网易云语义：standard/exhigh/lossless/hires 为三平台公共档；
    # QQ 独有 jymaster(臻品母带)/ogg640；网易云独有 jymaster/jyeffect/dolby/
    # vivid/sky（后四档不进降级链）；合法值域见 routes/api.py 的 _VALID_LEVELS
    "level_netease": "",
    "level_qq": "",
    "level_kugou": "",
    # 目标音质档取不到流时自动向低音质档回退
    "enable_quality_fallback": "true",
    "write_metadata": "true",
    "write_lyric": "true",
    "auto_sync_enabled": "true",
    # 同步时间点：多个 "HH:MM" 用逗号分隔
    "sync_times": "03:00,09:00,21:00",
    # 同步抖动延迟（秒，0-3600），触发后随机延迟 0~N 秒再执行
    "sync_jitter": "600",
    "max_retries": "3",
    # 添加歌单时的默认下载数量
    "default_playlist_limit": "50",
    # 多账号下载模式：fallback(接力) / round_robin(轮询)
    "download_mode": "fallback",
    # 优先使用非VIP账号下载（仅 VIP 歌曲才用 VIP 账号）
    "prefer_non_vip": "false",
    # 单账号每自然小时下载成功上限（0=不限制）
    "hourly_limit_per_account": "50",
    # QQ cookie 自动保活间隔（小时，0=关闭）：定期调桥接服务刷新 musickey，
    # 避免 QQ 网页 cookie 长期不刷新而提前失效（名义 60 天，不刷新约 1 天即失效）
    "qq_keepalive_hours": "6",
    # 排除歌曲关键字（英文逗号分隔，如 "live,伴奏,remix"）
    "exclude_keywords": "",
    # 排除过滤应用范围：逗号分隔，如 "playlist,search"（两者都应用）/ "playlist" / "search" / ""（都不应用）
    "exclude_scope": "playlist,search",
    # 音乐整理：清理重复文件时是否直接删除（false=移入下载目录 .trash 回收站）
    "organize_direct_delete": "false",
}


def get_custom_api_url() -> str:
    """读取自定义API服务URL（需在 app context 内调用）

    反环硬约束：此函数不得 import core.providers.*。
    """
    if Setting.get("use_custom_api_url", "false") != "true":
        return ""
    return Setting.get("custom_api_url", "").rstrip("/")


def get_qq_custom_api_url() -> str:
    """读取QQ音乐自定义API服务URL（需在 app context 内调用）

    未勾选 use_custom_qq_api_url 时返回空串（走内置 qqmusic-api bridge）。
    反环硬约束：此函数不得 import core.providers.*。
    """
    if Setting.get("use_custom_qq_api_url", "false") != "true":
        return ""
    return Setting.get("qq_api_base_url", "").rstrip("/")


def get_kugou_custom_api_url() -> str:
    """读取酷狗音乐自定义API服务URL（需在 app context 内调用）

    未勾选 use_custom_kugou_api_url 时返回空串（走内置 kugou-api bridge）。
    反环硬约束：此函数不得 import core.providers.*。
    """
    if Setting.get("use_custom_kugou_api_url", "false") != "true":
        return ""
    return Setting.get("kugou_api_base_url", "").rstrip("/")


def get_api_base_url(platform: str) -> str:
    """按平台读取 API 服务地址（需在 app context 内调用）

    返回空串表示走平台内置 bridge：
    - qq：use_custom_qq_api_url 勾选时返回 qq_api_base_url，否则空（内置服务）
    - kugou：use_custom_kugou_api_url 勾选时返回 kugou_api_base_url，否则空（内置服务）
    - 其他（netease 等）：沿用网易云自定义 API 逻辑（含 use_custom_api_url
      开关，为空时走内置 bridge）

    反环硬约束：此函数不得 import core.providers.*。
    """
    if platform == "qq":
        return get_qq_custom_api_url()
    if platform == "kugou":
        return get_kugou_custom_api_url()
    return get_custom_api_url()


# 旧库补列迁移表：(表, 列, 列定义)。存量行补默认值，读取侧空值按各字段
# 语义回退（如 download_tasks 音质快照为空时回退 JOIN 的 Song，不回填不编造）
_ADDED_COLUMNS = (
    ("songs", "account_id", "INTEGER"),
    ("download_tasks", "account_id", "INTEGER"),
    ("accounts", "platform", "VARCHAR(20) DEFAULT 'netease' NOT NULL"),
    ("songs", "platform", "VARCHAR(20) DEFAULT 'netease' NOT NULL"),
    ("download_tasks", "platform", "VARCHAR(20) DEFAULT 'netease' NOT NULL"),
    ("playlists", "platform", "VARCHAR(20) DEFAULT 'netease' NOT NULL"),
    # 下载时刻音质/文件快照三列
    ("download_tasks", "quality", "VARCHAR(20) DEFAULT '' NOT NULL"),
    ("download_tasks", "file_path", "VARCHAR(500) DEFAULT '' NOT NULL"),
    ("download_tasks", "file_size", "INTEGER DEFAULT 0 NOT NULL"),
    # 任务类型（存量任务均为普通下载）
    ("download_tasks", "kind", "VARCHAR(20) DEFAULT 'normal' NOT NULL"),
)


def _column_exists(inspector, table: str, column: str) -> bool:
    """检查某列是否已存在（用于 ALTER TABLE 迁移）"""
    return column in {c["name"] for c in inspector.get_columns(table)}


def _column_is_integer(inspector, table: str, column: str) -> bool:
    """检查某列是否为 INTEGER 类型（用于触发表重建迁移）"""
    if not inspector.has_table(table):
        return False
    for c in inspector.get_columns(table):
        if c["name"] == column:
            return "INTEGER" in str(c["type"]).upper()
    return False


def _migrate_song_ids_to_text(engine) -> None:
    """songs.id / download_tasks.song_id 列 Integer → VARCHAR(64)（SQLite 表重建迁移）

    QQ 音乐歌曲 ID（songmid）为字符串（如 003rJSwm3TechU），数据库需以文本存储；
    且 SQLite 中 INTEGER 值与 TEXT 值不相等，旧数字 id 必须 CAST 为 TEXT 才能被
    str 化后的查询命中。SQLite 不支持 ALTER COLUMN，采用建新表 → 复制 → 删旧表
    → 改名，单事务执行，失败整体回滚。新库由 create_all 直接建出 VARCHAR 列，
    本迁移自动跳过。必须在补列迁移（platform/account_id 等）之后执行。
    """
    inspector = inspect(engine)

    if _column_is_integer(inspector, "songs", "id"):
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE songs_new (
                    id VARCHAR(64) NOT NULL,
                    platform VARCHAR(20) DEFAULT 'netease' NOT NULL,
                    name VARCHAR(300) NOT NULL,
                    artists VARCHAR(300),
                    album VARCHAR(300),
                    duration_ms INTEGER,
                    quality VARCHAR(20),
                    file_path VARCHAR(500),
                    file_size INTEGER,
                    playlist_id INTEGER,
                    downloaded_at DATETIME,
                    status VARCHAR(20),
                    error_msg VARCHAR(500),
                    source_name VARCHAR(200),
                    account_id INTEGER,
                    PRIMARY KEY (id),
                    FOREIGN KEY(playlist_id) REFERENCES playlists (id)
                )
            """))
            conn.execute(text("""
                INSERT INTO songs_new
                    (id, platform, name, artists, album, duration_ms, quality,
                     file_path, file_size, playlist_id, downloaded_at, status,
                     error_msg, source_name, account_id)
                SELECT CAST(id AS TEXT), platform, name, artists, album, duration_ms,
                       quality, file_path, file_size, playlist_id, downloaded_at,
                       status, error_msg, source_name, account_id
                FROM songs
            """))
            conn.execute(text("DROP TABLE songs"))
            conn.execute(text("ALTER TABLE songs_new RENAME TO songs"))
        print("[init_db] songs.id 已迁移为 VARCHAR(64)（song_id 字符串化）")

    # 重新 inspect（上一张表的结构变更已生效）
    inspector = inspect(engine)
    if _column_is_integer(inspector, "download_tasks", "song_id"):
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE download_tasks_new (
                    pk INTEGER NOT NULL,
                    platform VARCHAR(20) DEFAULT 'netease' NOT NULL,
                    song_id VARCHAR(64) NOT NULL,
                    song_name VARCHAR(300),
                    artists VARCHAR(300),
                    playlist_id INTEGER,
                    playlist_name VARCHAR(200),
                    status VARCHAR(20),
                    progress INTEGER,
                    error_msg VARCHAR(500),
                    account_id INTEGER,
                    fee INTEGER,
                    quality VARCHAR(20) DEFAULT '' NOT NULL,
                    file_path VARCHAR(500) DEFAULT '' NOT NULL,
                    file_size INTEGER DEFAULT 0 NOT NULL,
                    kind VARCHAR(20) DEFAULT 'normal' NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME,
                    PRIMARY KEY (pk)
                )
            """))
            conn.execute(text("""
                INSERT INTO download_tasks_new
                    (pk, platform, song_id, song_name, artists, playlist_id,
                     playlist_name, status, progress, error_msg, account_id, fee,
                     quality, file_path, file_size, kind, created_at, updated_at)
                SELECT pk, platform, CAST(song_id AS TEXT), song_name, artists,
                       playlist_id, playlist_name, status, progress, error_msg,
                       account_id, fee, quality, file_path, file_size, kind,
                       created_at, updated_at
                FROM download_tasks
            """))
            conn.execute(text("DROP TABLE download_tasks"))
            conn.execute(text("ALTER TABLE download_tasks_new RENAME TO download_tasks"))
        print("[init_db] download_tasks.song_id 已迁移为 VARCHAR(64)（song_id 字符串化）")


def _migrate_song_pk_to_composite(engine) -> None:
    """songs.id 单列主键 → (id, platform) 复合主键（SQLite 表重建迁移，幂等）

    表重建写法复用 _migrate_song_ids_to_text 的既有套路：建新表 → 显式列名
    复制 → DROP → RENAME，单事务失败整体回滚。必须在 platform 归一化 UPDATE
    之后调用（songs_new.platform 为 NOT NULL，遗留 NULL/空 platform 的行会让
    INSERT 失败、整体回滚）。
    """
    inspector = inspect(engine)
    if not inspector.has_table("songs"):
        return
    pk_cols = set(inspector.get_pk_constraint("songs").get("constrained_columns") or [])
    if pk_cols == {"id", "platform"}:
        return                      # 已是复合主键，幂等跳过
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE songs_new (
                id VARCHAR(64) NOT NULL,
                platform VARCHAR(20) DEFAULT 'netease' NOT NULL,
                name VARCHAR(300) NOT NULL,
                artists VARCHAR(300),
                album VARCHAR(300),
                duration_ms INTEGER,
                quality VARCHAR(20),
                file_path VARCHAR(500),
                file_size INTEGER,
                playlist_id INTEGER,
                downloaded_at DATETIME,
                status VARCHAR(20),
                error_msg VARCHAR(500),
                source_name VARCHAR(200),
                account_id INTEGER,
                PRIMARY KEY (id, platform),
                FOREIGN KEY(playlist_id) REFERENCES playlists (id)
            )
        """))
        conn.execute(text("""
            INSERT INTO songs_new
                (id, platform, name, artists, album, duration_ms, quality,
                 file_path, file_size, playlist_id, downloaded_at, status,
                 error_msg, source_name, account_id)
            SELECT id, platform, name, artists, album, duration_ms, quality,
                   file_path, file_size, playlist_id, downloaded_at, status,
                   error_msg, source_name, account_id
            FROM songs
        """))                       # 显式列名（不依赖列顺序）；现数据按 id 唯一，无冲突
        conn.execute(text("DROP TABLE songs"))
        conn.execute(text("ALTER TABLE songs_new RENAME TO songs"))
    print("[init_db] songs 主键已迁移为 (id, platform) 复合主键")


def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Sqlite 连接级优化：WAL 允许读写并发（读不再被写锁阻塞），
    busy_timeout 让偶发锁冲突等待而不是立即抛 database is locked。
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def init_db(app, db_path: str = "downloads.db") -> None:
    """初始化数据库：配置 SQLAlchemy、创建表、写入默认配置、兼容迁移"""
    abs_db_path = Path(db_path).resolve()
    abs_db_path.parent.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{abs_db_path.as_posix()}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # 幂等保护：同进程多次 init_db()（测试/多 app）会重复注册 connect 监听，
    # 每次建立连接都会重复执行 PRAGMA
    if not event.contains(Engine, "connect", _set_sqlite_pragma):
        event.listen(Engine, "connect", _set_sqlite_pragma)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        # 兼容迁移：给旧表补列（顺序即历史版本的补列顺序；表重建迁移
        # _migrate_song_ids_to_text 依赖这些列已存在，必须先执行）
        inspector = inspect(db.engine)
        for table, column, ddl in _ADDED_COLUMNS:
            if inspector.has_table(table) and not _column_exists(inspector, table, column):
                with db.engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        # 兼容迁移：songs.id / download_tasks.song_id Integer → VARCHAR(64)
        # （QQ songmid 字符串化，须在上述补列迁移之后执行）
        _migrate_song_ids_to_text(db.engine)
        # 更新历史数据：确保所有记录都有正确的平台标记（残留 NULL/空 platform 归一到 netease）
        # 重新 inspect：Inspector 缓存列信息，补列/表重建后旧实例看不到新结构
        inspector = inspect(db.engine)
        for table in ("songs", "download_tasks", "playlists", "accounts"):
            if inspector.has_table(table) and _column_exists(inspector, table, "platform"):
                with db.engine.begin() as conn:
                    conn.execute(text(
                        f"UPDATE {table} SET platform = 'netease' WHERE platform IS NULL OR platform = ''"))
        # 写入缺失的默认配置
        for key, value in DEFAULT_SETTINGS.items():
            if not db.session.get(Setting, key):
                if key == "web_port":
                    value = default_web_port()
                db.session.add(Setting(key=key, value=value))
        db.session.commit()
        # 兼容迁移：songs.id 单列主键 → (id, platform) 复合主键
        # （必须在 platform 归一化 UPDATE 之后：songs_new.platform 为 NOT NULL）
        _migrate_song_pk_to_composite(db.engine)
        # N4c：常用查询索引（download_tasks 表重建迁移会删掉先建的索引，
        # 故必须在 _migrate_song_ids_to_text 之后执行）
        with db.engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_platform_song ON download_tasks (platform, song_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_status ON download_tasks (status)"))
