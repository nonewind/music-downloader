// 全局工具函数

// 显示 toast 提示（Frontend 2.0：ui-toast 结构，右上角，3 秒自动消失）
// 签名兼容旧调用 (msg, title)；kind 可选 'info'|'success'|'warning'|'error'
function showToast(msg, title = "提示", kind = "info") {
    const wrap = document.getElementById("global-toast");
    // 容器不是新版 ui-toast-wrap 时走旧 bootstrap toast 兜底（防御性，
    // 当前所有页面均经 base.html 提供新容器）
    if (!wrap || !wrap.classList.contains("ui-toast-wrap")) {
        _showToastLegacy(msg, title);
        return;
    }
    const el = document.createElement("div");
    el.className = "ui-toast" + (kind !== "info" ? " ui-toast-" + kind : "");
    el.setAttribute("role", "status");

    const body = document.createElement("div");
    body.className = "ui-toast-body";
    const t = document.createElement("span");
    t.className = "ui-toast-title";
    t.textContent = title;
    body.appendChild(t);
    body.appendChild(document.createTextNode(String(msg)));

    const close = document.createElement("button");
    close.type = "button";
    close.className = "ui-toast-close";
    close.setAttribute("aria-label", "关闭");
    close.innerHTML = "&times;";

    el.appendChild(body);
    el.appendChild(close);
    wrap.appendChild(el);

    let closed = false;
    const dismiss = () => {
        if (closed) return;
        closed = true;
        el.classList.remove("ui-toast-show");
        setTimeout(() => el.remove(), 220);  // 等入场同款过渡播完再移除
    };
    close.addEventListener("click", dismiss);
    // 双 rAF 确保初始样式已提交，入场过渡才能触发
    requestAnimationFrame(() =>
        requestAnimationFrame(() => el.classList.add("ui-toast-show"))
    );
    setTimeout(dismiss, 3000);
}

// 旧版 bootstrap toast 兜底路径（结构依赖 #toast-title/#toast-body）
function _showToastLegacy(msg, title) {
    const toastEl = document.getElementById("global-toast");
    const titleEl = document.getElementById("toast-title");
    const bodyEl = document.getElementById("toast-body");
    if (!toastEl || !titleEl || !bodyEl || typeof bootstrap === "undefined") return;
    titleEl.textContent = title;
    bodyEl.textContent = msg;
    bootstrap.Toast.getOrCreateInstance(toastEl, { delay: 3000 }).show();
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

// 更新同步指示器（Frontend 2.0：改用 ui-badge 徽章类，与全站视觉统一；
// sync-active 闪烁动画类保留，底栏摘要等其余逻辑不变）
function updateSyncIndicator(active) {
    const el = document.getElementById("sync-indicator");
    if (!el) return;
    if (active) {
        el.innerHTML = '<span class="ui-badge ui-badge-success sync-active">下载中...</span>';
    } else {
        el.innerHTML = '<span class="ui-badge ui-badge-muted">空闲</span>';
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

// ============================================================
// Frontend 2.0 组件层（UI-02）
// ============================================================
// 本段只增不改不删：上方既有函数被未迁移页面依赖，保留至迁移完成。
// 新函数自本包起是新页面的唯一标准（format 家族以 fmt* 为准）。

// ---- Alpine 全局 store：模态框开关（macros/ui.html 的 ui.modal 约定）----
// 用法：页面 Alpine 表达式内 `$store.modals.<id> = true/false`，
// 页面 JS 内 `Alpine.store('modals').<id> = true/false`。
// 多个 modal 各占一个 store key，互不干扰。
document.addEventListener("alpine:init", () => {
    if (window.Alpine && !Alpine.store("modals")) {
        Alpine.store("modals", {});
    }
});

// ---- 确认框 ----
// window.uiConfirm({title, message, confirmText, cancelText, danger}) -> Promise<boolean>
// 动态构建 ui-modal DOM（原生实现，不依赖 Alpine）；ESC / 遮罩点击 / 关闭 /
// 取消 = false，确认 = true。不替换任何现有 confirm() 调用（由后续迁移包统一改）。
window.uiConfirm = function(opts = {}) {
    const {
        title = "确认操作",
        message = "",
        confirmText = "确定",
        cancelText = "取消",
        danger = false,
    } = opts;
    return new Promise((resolve) => {
        let done = false;

        const mask = document.createElement("div");
        mask.className = "ui-modal-mask";
        mask.style.display = "flex";

        const modal = document.createElement("div");
        modal.className = "ui-modal";
        modal.setAttribute("role", "dialog");
        modal.setAttribute("aria-modal", "true");

        const header = document.createElement("div");
        header.className = "ui-modal-header";
        const h = document.createElement("h3");
        h.textContent = title;
        const closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "ui-modal-close";
        closeBtn.setAttribute("aria-label", "关闭");
        closeBtn.innerHTML = "&times;";
        header.appendChild(h);
        header.appendChild(closeBtn);

        const body = document.createElement("div");
        body.className = "ui-modal-body";
        body.textContent = message;

        const footer = document.createElement("div");
        footer.className = "ui-modal-footer";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "ui-btn ui-btn-secondary";
        cancel.textContent = cancelText;
        const ok = document.createElement("button");
        ok.type = "button";
        ok.className = "ui-btn " + (danger ? "ui-btn-danger" : "ui-btn-primary");
        ok.textContent = confirmText;
        footer.appendChild(cancel);
        footer.appendChild(ok);

        modal.appendChild(header);
        modal.appendChild(body);
        modal.appendChild(footer);
        mask.appendChild(modal);
        document.body.appendChild(mask);

        const finish = (result) => {
            if (done) return;
            done = true;
            document.removeEventListener("keydown", onKey);
            mask.remove();
            resolve(result);
        };
        const onKey = (e) => {
            if (e.key === "Escape") finish(false);
        };

        document.addEventListener("keydown", onKey);
        mask.addEventListener("click", (e) => {
            if (e.target === mask) finish(false);  // 仅遮罩本体，点击弹窗内部不算
        });
        closeBtn.addEventListener("click", () => finish(false));
        cancel.addEventListener("click", () => finish(false));
        ok.addEventListener("click", () => finish(true));
        ok.focus();
    });
};

// ---- 按钮加载态 ----
// window.withButtonLoading(btn, asyncFn)：备份按钮 HTML → 换 spinner+禁用 →
// await asyncFn → 恢复（finally，异常也恢复）。返回 asyncFn 的返回值。
window.withButtonLoading = async function(btn, asyncFn) {
    if (!btn) return asyncFn();
    const original = btn.innerHTML;
    btn.classList.add("is-loading");
    btn.disabled = true;
    btn.innerHTML = '<span class="ui-spinner"></span>';
    try {
        return await asyncFn();
    } finally {
        btn.classList.remove("is-loading");
        btn.disabled = false;
        btn.innerHTML = original;
    }
};

// ---- 统一 format 家族（新页面唯一标准；旧 formatSize/formatDuration 保留不动）----

// 时长（毫秒）→ 补零 mm:ss；满 1 小时进位为 h:mm:ss；无效输入返回 "--"
function fmtDurationMs(ms) {
    if (ms === null || ms === undefined || isNaN(ms) || ms < 0) return "--";
    let s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600);
    s -= h * 3600;
    const m = Math.floor(s / 60);
    s -= m * 60;
    const two = (n) => String(n).padStart(2, "0");
    return h > 0 ? h + ":" + two(m) + ":" + two(s) : two(m) + ":" + two(s);
}

// 字节数 → 智能单位（B/KB/MB/GB）；无效输入返回 "--"
function fmtSizeBytes(n) {
    if (n === null || n === undefined || isNaN(n) || n < 0) return "--";
    if (n < 1024) return n + " B";
    const kb = n / 1024;
    const mb = kb / 1024;
    const gb = mb / 1024;
    if (gb >= 1) return gb.toFixed(2) + " GB";
    if (mb >= 1) return mb.toFixed(1) + " MB";
    return kb.toFixed(1) + " KB";
}

// 日期输入自动判别：Date 对象 / epoch 毫秒或秒（含纯数字字符串）/ ISO 字符串
// → Date 或 null（无法解析）
function _fmtParseDate(v) {
    if (v === null || v === undefined || v === "") return null;
    if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
    if (typeof v === "number") {
        // 13 位量级视为毫秒，10 位量级视为秒
        const d = new Date(v < 1e12 ? v * 1000 : v);
        return isNaN(d.getTime()) ? null : d;
    }
    if (typeof v === "string") {
        const s = v.trim();
        if (/^\d+$/.test(s)) {
            const num = Number(s);
            const d = new Date(num < 1e12 ? num * 1000 : num);
            return isNaN(d.getTime()) ? null : d;
        }
        const d = new Date(s);
        return isNaN(d.getTime()) ? null : d;
    }
    return null;
}

// → YYYY-MM-DD
function fmtDate(v) {
    const d = _fmtParseDate(v);
    if (!d) return "--";
    const two = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate());
}

// → YYYY-MM-DD HH:mm:ss
function fmtDateTime(v) {
    const d = _fmtParseDate(v);
    if (!d) return "--";
    const two = (n) => String(n).padStart(2, "0");
    return fmtDate(d) + " " + two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
}

// ---- 平台元信息（单一来源）----
// 名称与后端 AVAILABLE_PLATFORMS/PLATFORM_NAMES 一致；color 指向 Design
// Token 平台色。后续页面逐步把散落的平台名/色值切换到此处。
window.PLATFORM_META = {
    netease: { name: "网易云", color: "var(--ui-plat-netease)" },
    qq:      { name: "QQ音乐", color: "var(--ui-plat-qq)" },
    kugou:   { name: "酷狗音乐", color: "var(--ui-plat-kugou)" },
};
