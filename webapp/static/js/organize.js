// 音乐整理页逻辑（重复文件扫描 / 回收站清理）
// 依赖全局工具（app.js 先于本脚本加载）：api()（内部已按 (window.APP_BASE||"")
// 拼接前缀并处理 401/403/code!=0）、escapeHtml()、showToast()
// —— 与 history.js 同款约定，页内不另定义副本。

// 扫描结果存内存（radio 修改只改 keep，不重建 DOM 数据源）：
// [{ group_type, recommended, keep, items: [...] }]
let groups = [];
// 删除模式：true=直接删除；false=移入 .trash 回收站（读自 /api/settings 的
// organize_direct_delete，与后端 clean 端点同款判定）
let directDelete = false;

const groupList = document.getElementById("group-list");
const emptyHint = document.getElementById("empty-hint");
const cleanBar = document.getElementById("clean-bar");
const cleanCount = document.getElementById("clean-count");

// ============================================================
// 删除模式提示（回收站 / 直接删除）
// ============================================================
async function loadMode() {
    const badge = document.getElementById("organize-mode-badge");
    const hint = document.getElementById("mode-hint");
    try {
        const data = await api("/api/settings");
        directDelete = (data.data?.organize_direct_delete || "false") === "true";
    } catch (e) {
        // 读取失败按默认值（回收站模式）提示，不阻塞扫描功能
        directDelete = false;
    }
    if (directDelete) {
        badge.className = "badge bg-danger";
        badge.textContent = "直接删除模式";
        hint.textContent = "当前为直接删除模式：清理的文件将被永久删除，不会进入回收站。";
    } else {
        badge.className = "badge bg-secondary";
        badge.textContent = "回收站模式";
        hint.textContent = "当前为回收站模式：清理的文件移入下载目录下 .trash，可手动恢复。";
    }
}

// ============================================================
// 扫描
// ============================================================
async function scan() {
    const btn = document.getElementById("btn-scan");
    if (btn.disabled) return;  // 防重入：扫描期间按钮已禁用
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>扫描中...';
    try {
        // 全量扫描解析音频元数据可能较慢，覆盖默认 15s 超时
        const data = await api("/api/organize/scan", { method: "POST", timeout: 300000 });
        const d = data.data || {};
        // 每组默认保留 recommended；用户改选只更新 keep 字段
        groups = (d.groups || []).map(g => ({
            group_type: g.group_type,
            recommended: g.recommended,
            keep: g.recommended,
            items: g.items || [],
        }));
        renderSummary(d.scanned || 0, groups.length, d.duration_s);
        renderGroups();
    } catch (e) {
        // 403 时后端 msg 为「仅管理员可访问」，api() 已转成 Error.message
        showToast(e.message, "错误");
    } finally {
        btn.disabled = false;
        btn.innerHTML = original;
    }
}

function renderSummary(scanned, nGroups, durationS) {
    const el = document.getElementById("scan-summary");
    el.innerHTML = `共扫描 <strong>${Number(scanned) || 0}</strong> 个音频文件 · ` +
        `<strong>${Number(nGroups) || 0}</strong> 组重复 · 耗时 ${escapeHtml(String(durationS ?? "--"))} 秒` +
        (nGroups ? "" : "，未发现重复文件。");
    el.classList.remove("d-none");
}

// ============================================================
// 分组卡片渲染（所有后端字符串经 escapeHtml 后入 DOM）
// ============================================================
const GROUP_META = {
    same_id: { text: "同歌曲多版本", cls: "bg-primary" },
    identical: { text: "完全重复文件", cls: "bg-warning" },
};

// 规格列：无损显示 采样率/位深（如 FLAC 96kHz/24bit），有损显示码率（如 MP3 320kbps）
function formatSpec(it) {
    const ext = String(it.ext || "").replace(".", "").toUpperCase();
    const sr = Number(it.sample_rate) || 0;
    const bits = Number(it.bits_per_sample) || 0;
    const kbps = Number(it.bitrate_kbps) || 0;
    const khz = sr > 0 ? (sr / 1000).toFixed(sr % 1000 ? 1 : 0) + "kHz" : "";
    if (it.lossless && khz) {
        return `${ext} ${khz}${bits ? "/" + bits + "bit" : ""}`;
    }
    if (kbps) return `${ext} ${kbps}kbps`;
    return ext || "--";
}

// 大小：MB 两位小数
function formatSizeMB(bytes) {
    return ((Number(bytes) || 0) / 1024 / 1024).toFixed(2) + " MB";
}

// 时长：mm:ss
function fmtDuration(ms) {
    const s = Math.floor((Number(ms) || 0) / 1000);
    if (s <= 0) return "--";
    const m = Math.floor(s / 60);
    return String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}

// 修改时间：YYYY-MM-DD（后端 mtime 为 epoch 秒）
function fmtDate(epochSec) {
    const d = new Date((Number(epochSec) || 0) * 1000);
    if (!epochSec || isNaN(d.getTime())) return "--";
    const p = n => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}

function renderGroups() {
    emptyHint.classList.toggle("d-none", groups.length > 0);
    cleanBar.classList.toggle("d-none", groups.length === 0);
    if (!groups.length) {
        groupList.innerHTML = "";
        updateCleanCount();
        return;
    }
    groupList.innerHTML = groups.map((g, idx) => {
        const meta = GROUP_META[g.group_type] || { text: g.group_type, cls: "bg-secondary" };
        const first = g.items[0] || {};
        const label = [first.title, first.artist].filter(Boolean).join(" - ") || first.filename || "未知歌曲";
        const rows = g.items.map(it => {
            const isKeep = it.path === g.keep;
            const isRec = it.path === g.recommended;
            return `
                <tr>
                    <td><input class="form-check-input" type="radio" name="keep-${idx}"
                        data-group="${idx}" value="${escapeHtml(it.path)}"
                        ${isKeep ? "checked" : ""} title="保留此项"></td>
                    <td><span title="${escapeHtml(it.path)}">${escapeHtml(it.filename)}</span></td>
                    <td><small>${escapeHtml(formatSpec(it))}</small></td>
                    <td>${escapeHtml(formatSizeMB(it.size))}</td>
                    <td>${escapeHtml(fmtDuration(it.duration_ms))}</td>
                    <td><small>${escapeHtml(fmtDate(it.mtime))}</small></td>
                    <td>${isRec ? '<span class="badge bg-success">★推荐</span>' : ""}</td>
                </tr>`;
        }).join("");
        return `
            <div class="card mb-3">
                <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
                    <span>
                        <span class="badge ${meta.cls} me-2">${escapeHtml(meta.text)}</span>
                        <strong>${escapeHtml(label)}</strong>
                        ${first.song_id ? `<span class="badge bg-info ms-2">ID: ${escapeHtml(first.song_id)}</span>` : ""}
                    </span>
                    <span class="text-muted small">${g.items.length} 个文件</span>
                </div>
                <div class="table-responsive">
                    <table class="table table-sm table-hover align-middle mb-0">
                        <thead>
                            <tr>
                                <th width="44" title="保留此项">保留</th>
                                <th>文件</th>
                                <th width="140">规格</th>
                                <th width="100">大小</th>
                                <th width="80">时长</th>
                                <th width="110">修改时间</th>
                                <th width="80"></th>
                            </tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
                <div class="card-footer text-muted small">
                    <i class="bi bi-info-circle"></i> 非保留项将被清理（推荐项为每组规格最高的文件）
                </div>
            </div>`;
    }).join("");
    updateCleanCount();
}

// 当前选择下待清理文件总数（底部操作条展示）
function updateCleanCount() {
    const { deletePaths } = collectPlan(false);
    cleanCount.textContent = `待清理 ${deletePaths.length} 个文件`;
}

// ============================================================
// 组内 keep 选择（事件委托：列表整体重建后绑定不丢失）
// ============================================================
groupList.addEventListener("change", e => {
    const radio = e.target.closest('input[type="radio"][data-group]');
    if (!radio) return;
    const g = groups[Number(radio.dataset.group)];
    if (g) g.keep = radio.value;
    updateCleanCount();
});

// ============================================================
// 清理
// ============================================================
// 汇总待删路径与 repair 映射；useRecommended=true 时全部组用 recommended，
// 否则用各组当前 keep
function collectPlan(useRecommended) {
    const deletePaths = [];
    const repair = {};
    groups.forEach(g => {
        const keep = useRecommended ? g.recommended : g.keep;
        g.items.forEach(it => {
            if (it.path !== keep) {
                deletePaths.push(it.path);
                repair[it.path] = keep;
            }
        });
    });
    return { deletePaths, repair };
}

async function collectAndClean(useRecommended) {
    const { deletePaths, repair } = collectPlan(useRecommended);
    if (!deletePaths.length) {
        showToast("没有需要清理的文件", "提示");
        return;
    }
    const msg = directDelete
        ? `将永久删除 ${deletePaths.length} 个文件（不可恢复），确定继续？`
        : `将清理 ${deletePaths.length} 个文件至回收站（.trash 目录，可手动恢复），确定继续？`;
    if (!confirm(msg)) return;

    // 清理期间禁用两个入口，避免并发重复提交
    const btns = [document.getElementById("btn-clean-recommended"),
                  document.getElementById("btn-clean-selected")];
    btns.forEach(b => { b.disabled = true; });
    try {
        const data = await api("/api/organize/clean", {
            method: "POST",
            body: JSON.stringify({ delete_paths: deletePaths, repair }),
            timeout: 300000,
        });
        renderCleanResult(data.data || {});
    } catch (e) {
        showToast(e.message, "错误");
    } finally {
        btns.forEach(b => { b.disabled = false; });
    }
}

function renderCleanResult(data) {
    const deleted = data.deleted || [];
    const failed = data.failed || [];
    const repaired = data.repaired || [];
    const el = document.getElementById("clean-result");
    document.getElementById("clean-result-body").innerHTML = `
        <p class="mb-2">成功清理 <strong class="text-success">${deleted.length}</strong> 个文件。</p>
        ${failed.length ? `
            <h6 class="text-danger"><i class="bi bi-x-circle"></i> 失败 ${failed.length} 个</h6>
            ${failed.map(f => `<div class="small text-danger">• ${escapeHtml(f.path)}：${escapeHtml(f.reason)}</div>`).join("")}` : ""}
        ${repaired.length ? `
            <h6 class="mt-2 text-primary"><i class="bi bi-arrow-repeat"></i> 歌曲记录修复 ${repaired.length} 条</h6>
            ${repaired.map(r => `<div class="small text-muted">• 歌曲 ${escapeHtml(r.song)} 的下载记录已指向保留文件 ${escapeHtml(r.to)}</div>`).join("")}` : ""}
        <button class="btn btn-outline-primary btn-sm mt-2" id="btn-rescan">
            <i class="bi bi-arrow-clockwise"></i> 重新扫描
        </button>`;
    el.classList.remove("d-none");
    el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    document.getElementById("btn-rescan").addEventListener("click", scan);
}

// ============================================================
// 绑定与初始化
// ============================================================
document.getElementById("btn-scan").addEventListener("click", scan);
document.getElementById("btn-clean-recommended").addEventListener("click", () => collectAndClean(true));
document.getElementById("btn-clean-selected").addEventListener("click", () => collectAndClean(false));

loadMode();
