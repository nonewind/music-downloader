"""页面路由 - 渲染 HTML 模板

单用户自用工具，无登录体系（v0.9.0.0 移除）：所有页面对本机/内网
访问者直接开放，访问控制依赖部署环境（fnOS 面板/内网边界），可选访问令牌
与敏感操作限制见 webapp/access.py。

Frontend 3.0 起页面收成 4 个：找歌（首页，含我的歌单）/ 下载 / 整理 /
设置（含账号）。旧网址 /discover、/playlists、/accounts 保留为跳转，
避免收藏夹和旧链接失效。
"""

from flask import Blueprint, jsonify, redirect, render_template, url_for

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
def discover():
    """找歌页（首页）：搜索 + 我的歌单 + 官方排行榜 + 热门歌单"""
    return render_template(
        "discover.html",
        active_page="discover",
        platforms=AVAILABLE_PLATFORMS,
        platform_names=PLATFORM_NAMES,
    )


@views_bp.route("/history")
def history():
    """下载页（进行中的任务 + 下载记录）"""
    return render_template("history.html", active_page="history")


@views_bp.route("/organize")
def organize():
    """整理页"""
    return render_template("organize.html", active_page="organize")


@views_bp.route("/settings")
def settings():
    """设置页（含账号）"""
    return render_template("settings.html", active_page="settings")


# ======================================================================
# 旧网址跳转
# ======================================================================
@views_bp.route("/discover")
@views_bp.route("/playlists")
def old_discover():
    """旧「发现」「歌单管理」页 → 找歌页"""
    return redirect(url_for("views.discover"))


@views_bp.route("/accounts")
def old_accounts():
    """旧「账号管理」页 → 设置页的账号栏"""
    return redirect(url_for("views.settings") + "#accounts")


@views_bp.route("/healthz")
def healthz():
    """健康检查（Docker HEALTHCHECK 用；免访问令牌，不含任何数据）"""
    return jsonify({"status": "ok"})
