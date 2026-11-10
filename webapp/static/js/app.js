// 全局工具函数

// 显示 toast 提示（ui-toast 结构，3 秒自动消失）
// 签名兼容旧调用 (msg, title)；kind 可选 'info'|'success'|'warning'|'error'
function showToast(msg, title = "提示", kind = "info") {
    const wrap = document.getElementById("global-toast");
    if (!wrap) return;  // 无容器（异常页面）时静默跳过
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

// API 请求封装（默认 15 秒超时；options.timeout 可覆盖。
// v0.9.0.0 移除登录体系：401/403 不再做登录/改密跳转，统一按错误抛出）
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
        // 非 2xx：后端异常页（如 HTML 500）在此拦截。
        // 否则 resp.json() 抛 SyntaxError 掩盖真实原因；且若 5xx 恰好返回
        // {"code":0,...} 形状的 body 还会被误判为成功
        if (!resp.ok) {
            let detail = "";
            try { detail = (await resp.json()).msg || ""; } catch (_) {}
            throw new Error(detail || `服务异常 (HTTP ${resp.status})`);
        }
        const data = await resp.json();
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

// ============================================================
// 全局下载状态轮询（所有页面通用）
// ============================================================
// 每个页面都有底部下载条和导航上的失败角标，所以轮询放在全局 app.js。
// 结果同时写进 Alpine 的 queue 仓库，页面可以按歌曲查到自己的任务进度
// （找歌页的搜索结果行用它显示“下载中 62%”）。
async function refreshGlobalTaskStatus() {
    try {
        const data = await api("/api/tasks");
        const tasks = data.data || [];
        const failedCount = data.failed_count || 0;
        const pausedAll = !!data.paused_all;

        // 第 1 步：写入全局仓库（Alpine 还没启动时跳过，下一轮再写）
        if (window.Alpine && Alpine.store("queue")) {
            Alpine.store("queue").tasks = tasks;
            Alpine.store("queue").failed = failedCount;
        }

        // 第 2 步：渲染下载条和失败角标
        const batch = { done: data.batch_done, total: data.batch_total };
        renderDlbar(tasks, pausedAll, failedCount, batch);

        // 第 3 步：广播队列变化，让页面刷新自己的行状态
        broadcastQueueChange(tasks, pausedAll);
    } catch (e) {
        // 静默失败：下载条是辅助信息，不应弹错提示
        console.error("刷新任务状态失败:", e);
    }
}

// 队列变化广播：
//   md:task-finished  有任务从队列里消失（下完、失败或被删）
//   md:queue-idle     队列从有任务变成全空
// 找歌页监听这两个事件，只查本地库刷新搜索结果行的状态
let _prevTaskPks = new Set();
let _prevHasActive = false;
function broadcastQueueChange(tasks, pausedAll) {
    const pks = new Set(tasks.map(t => t.pk));
    let someFinished = false;
    for (const pk of _prevTaskPks) {
        if (!pks.has(pk)) someFinished = true;
    }
    _prevTaskPks = pks;
    if (someFinished) {
        window.dispatchEvent(new CustomEvent("md:task-finished"));
    }

    const hasActive = tasks.length > 0;
    if (_prevHasActive && !hasActive && !pausedAll) {
        window.dispatchEvent(new CustomEvent("md:queue-idle"));
    }
    _prevHasActive = hasActive || pausedAll;
}

// ============================================================
// 底部下载条 + 导航失败角标（由 refreshGlobalTaskStatus 每 2 秒驱动）
// ============================================================
// 下载条只在有任务时出现：左边是这一批的整体进度（已完成 / 总数），
// 右边是正在下的歌名和失败数；进度条按整批算，当前这首的进度也折算进去
let _prevFailedCount = null;
function renderDlbar(tasks, pausedAll, failedCount, batch) {
    // 第 1 步：导航上的失败角标
    const badge = document.getElementById("navFailBadge");
    const badgeNum = document.getElementById("navFailNum");
    if (badge && badgeNum) {
        badgeNum.textContent = String(failedCount);
        badge.classList.toggle("d-none", failedCount === 0);
        // 失败数变多时角标跳一下：先去掉类、强制重排，再加回去才能重播动画
        // （页面刚打开的第一次不算）
        if (_prevFailedCount !== null && failedCount > _prevFailedCount) {
            badge.classList.remove("is-bump");
            void badge.offsetWidth;
            badge.classList.add("is-bump");
        }
        _prevFailedCount = failedCount;
    }

    // 第 2 步：没有任务就收起下载条
    const title = document.getElementById("dlbarTitle");
    const side = document.getElementById("dlbarSide");
    const prog = document.getElementById("dlbarProg");
    if (!title || !side || !prog) return;
    if (tasks.length === 0) {
        document.body.classList.remove("has-dlbar");
        return;
    }

    // 第 3 步：整批进度 = (已完成数 + 当前这首的进度) / 总数
    const downloading = tasks.find(t => t.status === "downloading");
    const nPaused = tasks.filter(t => t.status === "paused").length;
    let songPart = 0;
    if (downloading) {
        songPart = Math.max(0, Math.min(100, downloading.progress)) / 100;
    }
    const pct = Math.floor((batch.done + songPart) / batch.total * 100);
    const countText = "已完成 " + batch.done + " / " + batch.total + " 首";

    // 第 4 步：左边标题
    let titleText = "";
    if (pausedAll) {
        titleText = "已全部暂停 · " + countText;
    } else if (!downloading && nPaused === tasks.length) {
        titleText = "已暂停 " + nPaused + " 首 · " + countText;
    } else {
        titleText = countText + " · " + pct + "%";
    }
    title.textContent = titleText;
    prog.style.width = pct + "%";

    // 第 5 步：右边是正在下的歌名（太长时省略）和失败数
    side.textContent = "";
    if (!pausedAll && downloading) {
        const song = document.createElement("span");
        song.className = "app-dlbar-song";
        song.textContent = "正在下 " + downloading.song_name;
        side.appendChild(song);
    }
    if (failedCount > 0) {
        if (side.childNodes.length) side.appendChild(document.createTextNode("·"));
        const fail = document.createElement("span");
        fail.className = "app-dlbar-fail";
        fail.textContent = failedCount + " 首失败";
        side.appendChild(fail);
    }
    side.appendChild(document.createTextNode("›"));

    document.body.classList.add("has-dlbar");
}

// DOM 就绪后启动轮询（兼容已加载完和未加载完两种情况）
function startGlobalStatusPolling() {
    // 立即刷新一次，让下载条尽快显示真实队列状态
    refreshGlobalTaskStatus();
    // 每 2 秒刷新一次；页面在后台时不发无谓请求，切回前台下一轮自动恢复
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
// 迁移已全部完成：本段为全站唯一样式的组件与工具层（ui-* 结构 +
// fmt* 格式化家族），旧零消费者函数已随 Bootstrap 一并移除。

// ---- Alpine 全局 store：模态框开关（macros/ui.html 的 ui.modal 约定）----
// 用法：页面 Alpine 表达式内 `$store.modals.<id> = true/false`，
// 页面 JS 内 `Alpine.store('modals').<id> = true/false`。
// 多个 modal 各占一个 store key，互不干扰。
// queue 仓库：全局任务轮询的结果（tasks 在途任务、failed 失败记录数）。
document.addEventListener("alpine:init", () => {
    if (window.Alpine && !Alpine.store("modals")) {
        Alpine.store("modals", {});
    }
    Alpine.store("queue", { tasks: [], failed: 0 });
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
        body.className = "ui-modal-body ui-modal-text";
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
            // 退场：遮罩淡出、弹窗缩回（样式见 app.css 的 is-leaving），播完再移除
            mask.classList.add("is-leaving");
            requestAnimationFrame(() => mask.classList.add("is-leaving-to"));
            setTimeout(() => mask.remove(), 200);
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

// ---- 统一 format 家族（全站唯一标准）----

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
