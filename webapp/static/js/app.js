// 全局工具函数

// 显示 toast 提示
function showToast(msg, title = "提示") {
    const toastEl = document.getElementById("global-toast");
    document.getElementById("toast-title").textContent = title;
    document.getElementById("toast-body").textContent = msg;
    const toast = bootstrap.Toast.getOrCreateInstance(toastEl, { delay: 3000 });
    toast.show();
}

// API 请求封装（默认 15 秒超时，401 自动跳转登录；options.timeout 可覆盖）
async function api(url, options = {}) {
    // 统一网关模式拼接前缀（window.APP_BASE 由 base.html 注入；普通模式为空串）
    const base = window.APP_BASE || "";
    const fullUrl = url.startsWith("/") ? base + url : url;
    const controller = new AbortController();
    const timeoutMs = options.timeout || 15000;
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const resp = await fetch(fullUrl, {
            headers: { "Content-Type": "application/json" },
            ...options,
            signal: controller.signal,
        });
        // 401 未登录：跳转登录页
        if (resp.status === 401) {
            window.location.href = base + "/login";
            return new Promise(() => {});  // 永不 resolve，避免后续逻辑报错
        }
        // 403 无权限：先识别"首登强制改密"拦截（后端带 must_change_password
        // 标记），跳改密页；其余按普通无权限抛错
        if (resp.status === 403) {
            let body403 = {};
            try { body403 = await resp.json(); } catch (_) {}
            if (body403.must_change_password) {
                window.location.href = base + "/change_password";
                return new Promise(() => {});  // 跳转后不 resolve，避免后续逻辑报错
            }
            throw new Error(body403.msg || "无权限执行此操作");
        }
        // 非 2xx：后端异常页（如 HTML 500）在此拦截。
        // 否则 resp.json() 抛 SyntaxError 掩盖真实原因；且若 5xx 恰好返回
        // {"code":0,...} 形状的 body 还会被误判为成功
        if (!resp.ok) {
            let detail = "";
            try { detail = (await resp.json()).msg || ""; } catch (_) {}
            throw new Error(detail || `服务异常 (HTTP ${resp.status})`);
        }
        const data = await resp.json();
        // 业务层 401 也跳转登录（兼容后端返回 200+code:401 的情况）
        if (data.code === 401) {
            window.location.href = base + "/login";
            return new Promise(() => {});
        }
        if (data.code !== 0) {
            throw new Error(data.msg || "请求失败");
        }
        return data;
    } catch (e) {
        if (e.name === "AbortError") {
            throw new Error("请求超时，请检查网络或服务状态");
        }
        throw e;
    } finally {
        clearTimeout(timer);
    }
}

// 简单 HTML 转义（防 XSS）
// app.js 经 base.html 先于各页脚本加载，全局可用（页内不再各自定义）
function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// 文件大小格式化
function formatSize(bytes) {
    if (!bytes) return "--";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

// ============================================================
// 音质档位中文名映射（TASK-04b）
// ============================================================
// 仅用于展示（历史列表音质列、重复下载确认弹窗）；设置页 select 的
// option 是表单值，不受此映射影响。文案取自设置页各平台选项的通用
// 部分（去掉平台相关 VIP 标注）；higher 不在设置页选项中但属降级链
// 档位（core.providers.base QUALITY_ORDER），历史记录可能出现，一并收录。
window.QUALITY_NAMES = {
    standard: "标准 128kbps",
    higher: "较高 192kbps",
    exhigh: "极高 320kbps",
    lossless: "无损 FLAC",
    hires: "Hi-Res",
    jymaster: "超清母带",
    ogg640: "OGG 640kbps",
    jyeffect: "高清臻音",
    dolby: "杜比全景声",
    vivid: "臻音全景声",
    sky: "沉浸环绕声",
};

// 档位中文名：已知档位返回映射，未知档位原样返回英文值，空值返回空串
// （调用方可用 `get_quality_name(v) || "--"` 兜底）
function get_quality_name(v) {
    if (!v) return "";
    return window.QUALITY_NAMES[v] || v;
}

// 全局设置缓存：任何页面加载过 /api/settings 后写入（见 playlists.js
// loadDefaultPlaylistLimit）；未加载过的页面保持空对象，取档位时返回空串
window.CACHED_SETTINGS = window.CACHED_SETTINGS || {};

// 当前设置音质的中文名（level_<platform> 未缓存时返回空串，由调用方
// 省略"当前设置音质（xx）"中的档位名）
function get_current_quality_name(platform) {
    const lv = window.CACHED_SETTINGS["level_" + platform] || "";
    return lv ? get_quality_name(lv) : "";
}

// 时长格式化
function formatDuration(ms) {
    if (!ms) return "--";
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return m + ":" + String(s % 60).padStart(2, "0");
}

// 状态徽章
function statusBadge(status) {
    const map = {
        success: '<span class="badge bg-success">成功</span>',
        failed: '<span class="badge bg-danger">失败</span>',
        skipped: '<span class="badge bg-secondary">已下载</span>',
        pending: '<span class="badge bg-warning">等待中</span>',
        downloading: '<span class="badge bg-primary">下载中</span>',
        paused: '<span class="badge bg-secondary">已暂停</span>',
        done: '<span class="badge bg-success">完成</span>',
    };
    return map[status] || '<span class="badge bg-secondary">' + status + '</span>';
}

// 更新同步指示器
function updateSyncIndicator(active) {
    const el = document.getElementById("sync-indicator");
    if (!el) return;
    if (active) {
        el.innerHTML = '<span class="badge bg-success sync-active">下载中...</span>';
    } else {
        el.innerHTML = '<span class="badge bg-secondary">空闲</span>';
    }
}

// ============================================================
// 全局下载状态轮询（所有页面通用）
// ============================================================
// 任何页面都会显示导航栏的"下载中/空闲"指示器，
// 因此轮询放在全局 app.js 中，而不是只在 dashboard.js 里。
async function refreshGlobalTaskStatus() {
    try {
        const data = await api("/api/tasks");
        const tasks = data.data || [];
        // 用服务端 has_active（存在未被暂停的在途任务）而非任务数量：
        // 「暂停全部」后任务仍在列表中，按数量判断会让导航栏恒显"下载中"，
        // 与用户刚点下的暂停语义冲突。判定逻辑放服务端可避免与后端语义漂移
        updateSyncIndicator(!!data.has_active);
        // 底部状态栏：队列摘要 + 当前下载项
        renderStatusbar(tasks, !!data.paused_all);
    } catch (e) {
        // 静默失败：指示器是辅助信息，不应弹错提示
        console.error("刷新任务状态失败:", e);
    }
}

// ============================================================
// 底部状态栏渲染（由 refreshGlobalTaskStatus 每 2 秒驱动）
// ============================================================
// 摘要段：下载中/排队/暂停分段计数（0 值段省略）；paused_all 优先展示
// 当前段：第一个 downloading 任务的「歌名 - 第一位歌手」+ 迷你进度条
function renderStatusbar(tasks, pausedAll) {
    const summary = document.getElementById("statusSummary");
    if (!summary) return;
    const nDownloading = tasks.filter(t => t.status === "downloading").length;
    const nPending = tasks.filter(t => t.status === "pending").length;
    const nPaused = tasks.filter(t => t.status === "paused").length;

    if (pausedAll) {
        summary.innerHTML = '<i class="bi bi-pause-circle"></i> 已全部暂停';
    } else if (nDownloading + nPending + nPaused > 0) {
        const segs = [];
        if (nDownloading) segs.push("下载中 " + nDownloading);
        if (nPending) segs.push("排队 " + nPending);
        if (nPaused) segs.push("暂停 " + nPaused);
        summary.innerHTML = '<i class="bi bi-download"></i> ' + segs.join(" · ");
    } else {
        summary.innerHTML = '<i class="bi bi-check-circle"></i> 队列空闲';
    }

    const current = document.getElementById("statusCurrent");
    if (!current) return;
    const downloading = tasks.find(t => t.status === "downloading");
    if (downloading) {
        const pct = Math.max(0, Math.min(100, downloading.progress || 0));
        const firstArtist = String(downloading.artists || "").split("/")[0].trim();
        current.innerHTML =
            '<span class="status-current-text">' + escapeHtml(downloading.song_name + " - " + firstArtist) + "</span>" +
            '<span class="status-progress"><span class="status-progress-bar" style="width:' + pct + '%"></span></span>';
        current.classList.remove("d-none");
    } else if (nPending > 0) {
        current.textContent = "待下载 " + nPending + " 首";
        current.classList.remove("d-none");
    } else {
        current.classList.add("d-none");
    }
}

// 点击/回车状态栏 → 下载页（APP_BASE 拼法与 api()/accounts.js 导出跳转一致）
const _appStatusbar = document.getElementById("appStatusbar");
if (_appStatusbar) {
    const _goHistory = () => {
        window.location.href = (window.APP_BASE || "") + "/history";
    };
    _appStatusbar.addEventListener("click", _goHistory);
    _appStatusbar.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            _goHistory();
        }
    });
}

// ============================================================
// 顶栏二级面包屑：各页切页内标签时调用
// ============================================================
// name 为空/null → 隐藏分隔符与子标题；否则写入文本并显示
// app.js 先于各页脚本加载，页内可直接调用 AppUI.setSubTab(...)
window.AppUI = {
    setSubTab(name) {
        const sep = document.querySelector(".crumb-sep");
        const sub = document.getElementById("crumbSub");
        if (!sep || !sub) return;
        if (!name) {
            sub.textContent = "";
            sep.classList.add("d-none");
            sub.classList.add("d-none");
        } else {
            sub.textContent = name;
            sep.classList.remove("d-none");
            sub.classList.remove("d-none");
        }
    },
};

// DOM 就绪后启动轮询（兼容已加载完和未加载完两种情况）
function startGlobalStatusPolling() {
    // 立即刷新一次，避免页面初始的"加载中..."停留过久
    refreshGlobalTaskStatus();
    // 每 2 秒刷新一次（与原 dashboard.js 间隔一致）；
    // 页面在后台时不发无谓请求，切回前台下一轮自动恢复
    setInterval(() => {
        if (document.hidden) return;
        refreshGlobalTaskStatus();
    }, 2000);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startGlobalStatusPolling);
} else {
    startGlobalStatusPolling();
}
