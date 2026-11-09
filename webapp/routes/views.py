"""页面路由 - 渲染 HTML 模板

单用户自用工具，无登录体系（v0.9.0.0 移除）：所有页面对本机/内网
访问者直接开放，访问控制依赖部署环境（fnOS 面板/内网边界）。
"""

from flask import Blueprint, render_template

from models import PLATFORM_NAMES

views_bp = Blueprint("views", __name__)

# 当前已实现的前端可用平台（配置驱动，后续新增平台在此追加即可）
AVAILABLE_PLATFORMS = [
    {"key": "netease", "name": "网易云", "icon": "music-note-beamed"},
    {"key": "qq", "name": "QQ音乐", "icon": "music-note-list"},
    {"key": "kugou", "name": "酷狗音乐", "icon": "vinyl"},
]


# ======================================================================
# 业务页面
# ======================================================================
@views_bp.route("/")
def dashboard():
    """总览页"""
    return render_template("dashboard.html", active_page="dashboard")


@views_bp.route("/discover")
def discover():
    """发现页（官方排行榜 / 热门歌单 / 搜索，配置驱动多平台）"""
    return render_template(
        "discover.html",
        active_page="discover",
        platforms=AVAILABLE_PLATFORMS,
        platform_names=PLATFORM_NAMES,
    )


@views_bp.route("/playlists")
def playlists():
    """歌单管理页"""
    return render_template(
        "playlists.html",
        active_page="playlists",
        platforms=AVAILABLE_PLATFORMS,
        platform_names=PLATFORM_NAMES,
    )


@views_bp.route("/accounts")
def accounts():
    """账号管理页"""
    return render_template("accounts.html", active_page="accounts")


@views_bp.route("/history")
def history():
    """下载历史页"""
    return render_template("history.html", active_page="history")


@views_bp.route("/settings")
def settings():
    """设置页"""
    return render_template("settings.html", active_page="settings")


@views_bp.route("/organize")
def organize():
    """音乐整理页"""
    return render_template("organize.html", active_page="organize")
