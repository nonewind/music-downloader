// 音乐整理页（ORG-04 工作台升级）：Alpine 单一状态入口 organizePage()
//
// 依赖全局工具（app.js 先于本脚本加载）：api()（内部处理 APP_BASE 前缀与
// 401/403/code!=0，默认 15s 超时——本页全部接口均提交即返回，无需长超时）、
// showToast()、fmtSizeBytes()、fmtDurationMs()、fmtDate()、get_quality_name()、
// window.PLATFORM_META。
// 渲染一律由 Alpine 模板绑定（x-for/x-text/:class），本文件不拼任何列表
// innerHTML（x-text 输出天然免转义）。
//
// 后端契约（ORG-01/02/03 后台任务化，本页纯消费端）：
//   GET  /api/organize/state      -> {active_job, last_scan, last_clean,
//                                     index:{total,active,missing}}
//   POST /api/organize/scan       -> {code:0,data:{job_id,status}}；
//                                    占用中/目录不存在 code:1 + msg
//   POST /api/organize/clean      -> 同上（提交即返回，逐文件结果经 jobs/<id>）
//   GET  /api/organize/jobs/<id>  -> {job:{id,job_type,status,progress,
//                                     processed,total,error_msg,result,
//                                     items(仅 clean，逐文件计划与结果)}}
//   GET  /api/organize/duplicates -> {groups:[{group_type,platform,song_id,
//                                     recommended,reason,items:[...其中
//                                     item.downloads={count,latest}]}]}
// 任务终态：success / partial / failed / interrupted。
// 轮询模式与 history.js 相同：定时器常开（1s），document.hidden 或上一轮
// 未返回时跳过本轮。离开页面后再回来：init 经 state 恢复在途任务续轮询，
// 分组/摘要一律从服务端读，不依赖内存。

// 整理任务终态集合（与后端 ORGANIZER_JOB_ACTIVE_STATUSES 互补）
const ORG_JOB_TERMINAL = ["success", "partial", "failed", "interrupted"];

function organizePage() {
    return {
        // ---- 唯一状态入口 ----
        loaded: false,        // init 完成标记（避免首帧闪空态）
        scanning: false,      // 扫描任务在途（轮询中）
        cleaning: false,      // 清理任务在途（轮询中）
        job: null,            // 当前轮询任务（jobs/<id> 返回的 job dict）；null=无任务
        groups: [],           // [{...后端组原样, files:[items], open, checked:Set(勾选path)}]
        selectedCount: 0,     // 勾选文件数（勾选变化时随 recomputeSummary 重算）
        selectedSongs: 0,     // 勾选涉及歌数（=有勾选的组数）
        freedBytes: 0,        // 勾选文件字节合计
        directDelete: null,   // true=永久删除模式；false=回收站模式；null=加载中
        scannedOnce: false,   // 已有可展示的扫描结果（区分「尚未扫描」与「无重复」空态）
        lastScan: null,       // 最近一次终态扫描任务（页头统计卡摘要）
        lastClean: null,      // 最近一次终态清理任务（页头统计卡摘要）
        index: { total: 0, active: 0, missing: 0 },  // 索引统计（state.index）
        result: null,         // 清理结果卡 {status,ok,trashed,deleted,failed,
                              //  failedItems,repaired,cleared}；null=不显示

        pollTimer: null,      // 1s 轮询定时器（常开，内部条件跳过）
        _polling: false,      // 防重入：上一轮请求未返回时跳过本轮

        // Alpine 组件初始化：读删除模式 + 恢复服务端状态（在途任务/上次结果）
        async init() {
            // 删除模式读取失败按回收站兜底，不阻塞页面
            try {
                const data = await api("/api/settings");
                this.directDelete = (data.data?.organize_direct_delete || "false") === "true";
            } catch (e) {
                this.directDelete = false;
            }
            try {
                const state = await api("/api/organize/state");
                const d = state.data || {};
                this.index = d.index || this.index;
                this.lastScan = d.last_scan || null;
                this.lastClean = d.last_clean || null;
                if (d.last_scan) this.scannedOnce = true;
                if (d.active_job) {
                    // 恢复在途任务：分组先按既有扫描结果展示，轮询接着推进
                    this.job = d.active_job;
                    this.scanning = this.job.job_type === "scan";
                    this.cleaning = this.job.job_type === "clean";
                    this.startPolling();
                }
            } catch (e) {
                showToast(e.message, "错误");
            }
            if (this.scannedOnce) {
                await this.loadGroups("recommended");
            }
            this.loaded = true;
        },

        // 离开页面（Alpine 组件销毁）时停掉轮询定时器
        destroy() {
            this.stopPolling();
        },

        // ============================================================
        // 任务轮询（扫描/清理共用，1s 间隔）
        // ============================================================
        startPolling() {
            this.stopPolling();
            this.pollTimer = setInterval(() => this.pollOnce(), 1000);
            this.pollOnce();      // 立即拉一次，尽快刷新进度
        },

        stopPolling() {
            if (this.pollTimer) {
                clearInterval(this.pollTimer);
                this.pollTimer = null;
            }
        },

        async pollOnce() {
            if (document.hidden || this._polling || !this.job) return;
            this._polling = true;
            try {
                const data = await api("/api/organize/jobs/" + this.job.id);
                const job = data.data && data.data.job;
                if (!job) return;
                this.job = job;
                if (ORG_JOB_TERMINAL.includes(job.status)) {
                    this.stopPolling();
                    if (job.job_type === "scan") this.finishScan(job);
                    else this.finishClean(job);
                }
            } catch (e) {
                // 单轮失败静默（网络抖动下一轮自动重试），toast 会打断轮询体验
                console.error("查询整理任务失败:", e);
            } finally {
                this._polling = false;
            }
        },

        // ============================================================
        // 扫描（提交即返回 job_id，进度走轮询）
        // ============================================================
        async scan() {
            if (this.scanning || this.cleaning) return;  // 防重入：任务运行中禁用
            this.scanning = true;    // 立即锁定按钮（提交期 + 轮询期全程）
            try {
                const data = await api("/api/organize/scan", { method: "POST" });
                const d = data.data || {};
                this.result = null;      // 新任务丢弃旧结果卡
                this.job = {
                    id: d.job_id, job_type: "scan", status: d.status,
                    progress: 0, processed: 0, total: 0,
                    error_msg: null, result: null,
                };
                this.startPolling();     // scanning 保持 true，终态由 finishScan 复位
            } catch (e) {
                this.scanning = false;   // 提交失败：解锁可重试
                // 占用中（已有整理任务）/目录不存在等：后端 msg 直接可读
                showToast(e.message, "错误");
            }
        },

        // 扫描终态：成功 → 刷新 state+分组并 toast 摘要；失败 → 错误原因
        async finishScan(job) {
            this.scanning = false;
            this.job = null;
            if (job.status === "success") {
                await this.refreshData("recommended");
                const summary = job.result || {};
                showToast("扫描完成：共 " + (summary.scanned ?? "--") + " 个文件，"
                          + this.groups.length + " 组重复", "扫描完成", "success");
            } else {
                showToast(job.error_msg || "扫描失败", "扫描失败", "error");
            }
        },

        // ============================================================
        // 数据加载（state 摘要 + duplicates 分组）
        // ============================================================
        async refreshData(preselect) {
            try {
                const state = await api("/api/organize/state");
                const d = state.data || {};
                this.index = d.index || this.index;
                this.lastScan = d.last_scan || null;
                this.lastClean = d.last_clean || null;
                if (d.last_scan) this.scannedOnce = true;
            } catch (e) {
                showToast(e.message, "错误");
            }
            await this.loadGroups(preselect);
        },

        // 分组展开：files 为后端 items；preselect="recommended" 时默认勾选
        // 除 recommended 外的全部文件（自由多选仍可后续改，含推荐）；
        // "none"=空选（清理完成后的刷新，与「成功后清空本次选择」一致）
        async loadGroups(preselect) {
            try {
                const data = await api("/api/organize/duplicates");
                this.groups = (data.data.groups || []).map(g => ({
                    ...g,
                    files: g.items || [],
                    open: true,
                    checked: preselect === "recommended"
                        ? new Set((g.items || [])
                            .filter(it => it.path !== g.recommended)
                            .map(it => it.path))
                        : new Set(),
                }));
            } catch (e) {
                showToast(e.message, "错误");
                this.groups = [];
            }
            this.recomputeSummary();
        },

        // ============================================================
        // 选择逻辑（自由多选：允许全勾/全不勾，不强制每组保留一个）
        // ============================================================
        applyRecommended() {
            for (const g of this.groups) {
                g.checked = new Set(g.files
                    .filter(it => it.path !== g.recommended)
                    .map(it => it.path));
            }
            this.recomputeSummary();
        },

        clearSelection() {
            for (const g of this.groups) g.checked = new Set();
            this.recomputeSummary();
        },

        toggleFile(g, it, on) {
            if (on) g.checked.add(it.path);
            else g.checked.delete(it.path);
            this.recomputeSummary();
        },

        groupCheckedCount(g) {
            let n = 0;
            for (const it of g.files) {
                if (g.checked.has(it.path)) n++;
            }
            return n;
        },

        isGroupAllChecked(g) {
            return g.files.length > 0 && g.files.every(it => g.checked.has(it.path));
        },

        // 组头快捷：已全选 → 全不选；否则全选
        toggleGroup(g) {
            g.checked = this.isGroupAllChecked(g)
                ? new Set()
                : new Set(g.files.map(it => it.path));
            this.recomputeSummary();
        },

        // 三个汇总一次性重算（勾选变化的唯一出口，模板零散绑定不重复计数）
        recomputeSummary() {
            let n = 0, bytes = 0, songs = 0;
            for (const g of this.groups) {
                let hit = false;
                for (const it of g.files) {
                    if (g.checked.has(it.path)) {
                        n++;
                        bytes += Number(it.size) || 0;
                        hit = true;
                    }
                }
                if (hit) songs++;
            }
            this.selectedCount = n;
            this.freedBytes = bytes;
            this.selectedSongs = songs;
        },

        // 当前勾选的文件扁平列表（Modal 预览与 delete_paths 同源）
        get selectedItems() {
            const rows = [];
            for (const g of this.groups) {
                for (const it of g.files) {
                    if (g.checked.has(it.path)) rows.push(it);
                }
            }
            return rows;
        },

        // ============================================================
        // 展示辅助（x-text 输出，天然免转义）
        // ============================================================
        // 组标题：same_id/candidate 显示 title - artist；identical 显示文件名
        songLabel(g) {
            const first = g.files[0] || {};
            if (g.group_type === "identical") return first.filename || "未知文件";
            return [first.title, first.artist].filter(Boolean).join(" - ")
                || first.filename || "未知歌曲";
        },

        // 组类型徽章（same_id_candidate 候选组用 warning 色区分）
        groupBadge(g) {
            if (g.group_type === "same_id") return { text: "同曲多版本", cls: "ui-badge-info" };
            if (g.group_type === "same_id_candidate") return { text: "候选", cls: "ui-badge-warning" };
            return { text: "内容完全相同", cls: "ui-badge-warning" };
        },

        // 组头副标题（小字）：平台/歌曲 ID 维度 + 推荐保留理由
        groupSub(g) {
            const parts = [];
            if (g.group_type === "same_id") {
                parts.push((this.platName(g.platform) || "未知平台") + "/" + (g.song_id ?? "?"));
            } else if (g.group_type === "same_id_candidate") {
                parts.push("song_id " + (g.song_id ?? "?") + "（来源平台未确认）");
            }
            if (g.reason) parts.push("推荐保留：" + g.reason);
            return parts.join(" · ");
        },

        platName(p) {
            if (!p) return "";
            const meta = window.PLATFORM_META[p];
            return (meta && meta.name) || p;
        },

        // 下载历史徽标：count>0 才显示；title 提示最新一次下载摘要
        dlBadge(it) {
            return (it.downloads && it.downloads.count > 0)
                ? "下载 ×" + it.downloads.count : "";
        },
        dlTip(it) {
            const l = it.downloads && it.downloads.latest;
            if (!l) return "";
            return [l.song_name, get_quality_name(l.quality) || l.quality, l.created_at]
                .filter(Boolean).join(" · ");
        },

        // 规格列：无损 `FLAC 96kHz/24bit` / 有损 `MP3 320kbps`；缺失段跳过
        //（不显示 0），全缺失显示 "--"
        formatSpec(it) {
            const ext = String(it.ext || "").replace(".", "").toUpperCase();
            const sr = Number(it.sample_rate) || 0;
            const bits = Number(it.bits_per_sample) || 0;
            const kbps = Number(it.bitrate_kbps) || 0;
            const khz = sr > 0 ? (sr / 1000).toFixed(sr % 1000 ? 1 : 0) + "kHz" : "";
            if (it.lossless && khz) return ext + " " + khz + (bits ? "/" + bits + "bit" : "");
            if (kbps) return ext + " " + kbps + "kbps";
            return ext || "--";
        },

        // ---- 页头统计卡 ----
        lastScanText() {
            if (!this.lastScan) return "未扫描";
            const r = this.lastScan.result || {};
            const time = this.lastScan.finished_at || this.lastScan.created_at || "--";
            if (this.lastScan.status !== "success") return time + " · 未完成";
            return time + " · " + (r.scanned ?? "--") + " 个文件";
        },

        lastCleanText() {
            if (!this.lastClean) return "未清理过";
            const r = this.lastClean.result || {};
            const ok = (r.deleted || 0) + (r.trashed || 0);
            const statusText = {
                success: "完成", partial: "部分完成",
                failed: "失败", interrupted: "中断",
            }[this.lastClean.status] || this.lastClean.status;
            const time = this.lastClean.finished_at || this.lastClean.created_at || "";
            return statusText + " " + ok + " 个 · 失败 " + (r.failed || 0)
                + (time ? " · " + time : "");
        },

        // 可整理 = 各组中推荐保留之外的文件数与字节合计（统计卡，不勾选联动）
        get tidyCount() {
            let n = 0;
            for (const g of this.groups) {
                for (const it of g.files) {
                    if (it.path !== g.recommended) n++;
                }
            }
            return n;
        },
        get tidyBytes() {
            let b = 0;
            for (const g of this.groups) {
                for (const it of g.files) {
                    if (it.path !== g.recommended) b += Number(it.size) || 0;
                }
            }
            return b;
        },

        jobLabel() {
            if (!this.job) return "";
            return this.job.job_type === "scan" ? "正在扫描下载目录" : "正在清理所选文件";
        },

        // ============================================================
        // 清理（Modal 提交 → 后台任务 → 轮询出结果卡）
        // ============================================================
        openCleanModal() {
            if (this.cleaning || this.scanning || !this.selectedCount) return;
            this.$store.modals.clean = true;
        },

        // repair 构造：组内存在未勾选项（有保留）时——每个待删 path 重指到
        // recommended（其未被勾选时）或第一个未勾选项；组内全部勾选（无保留）
        // 则这些 path 不进 repair（后端跳过联动，需重新下载）
        buildRepair() {
            const repair = {};
            for (const g of this.groups) {
                const unchecked = g.files.filter(it => !g.checked.has(it.path));
                if (!unchecked.length) continue;
                const keep = !g.checked.has(g.recommended)
                    ? g.recommended
                    : unchecked[0].path;
                for (const it of g.files) {
                    if (g.checked.has(it.path)) repair[it.path] = keep;
                }
            }
            return repair;
        },

        async confirmClean() {
            if (this.cleaning || this.scanning || !this.selectedCount) return;  // 防重复提交
            this.cleaning = true;    // 提交期间即锁定（防双击重复入队）
            try {
                // 提交即返回 job_id（默认 15s 超时足够），执行进度走轮询
                const data = await api("/api/organize/clean", {
                    method: "POST",
                    body: JSON.stringify({
                        delete_paths: this.selectedItems.map(it => it.path),
                        repair: this.buildRepair(),
                    }),
                });
                const d = data.data || {};
                this.$store.modals.clean = false;
                this.job = {
                    id: d.job_id, job_type: "clean", status: d.status,
                    progress: 0, processed: 0, total: this.selectedCount,
                    error_msg: null, result: null,
                };
                this.startPolling();     // cleaning 保持 true，终态由 finishClean 复位
            } catch (e) {
                this.cleaning = false;   // 提交失败：解锁 Modal 可重试
                // 请求异常：Modal 保持打开，错误经 toast 提示
                showToast(e.message, "错误");
            }
        },

        // 清理终态：结果卡（items+result 落账）→ toast → 清空选择 → 刷新
        async finishClean(job) {
            this.cleaning = false;
            const r = job.result || {};
            const items = job.items || [];
            const failedItems = items
                .filter(i => i.status === "failed")
                .map(i => ({ path: i.path, error_msg: i.error_msg }));
            const okCount = (r.deleted || 0) + (r.trashed || 0);
            this.result = {
                status: job.status,
                total: r.total ?? items.length,
                ok: okCount,
                trashed: r.trashed || 0,
                deleted: r.deleted || 0,
                failed: r.failed ?? failedItems.length,
                failedItems,
                repaired: r.repaired || [],
                cleared: r.cleared || [],
            };
            this.job = null;
            if (job.status === "success") {
                showToast("已清理 " + okCount + " 个文件（回收站 "
                          + (r.trashed || 0) + " / 直删 " + (r.deleted || 0) + "）",
                          "清理完成", "success");
            } else if (job.status === "partial") {
                showToast(failedItems.length + " 个文件清理失败，详见结果卡",
                          "部分失败", "warning");
            } else if (job.status === "interrupted") {
                showToast(job.error_msg || "清理任务被中断", "清理中断", "error");
            } else {
                showToast(job.error_msg || "清理全部失败", "清理失败", "error");
            }
            // 成功后清空本次选择；分组/摘要从服务端重读（已删文件自动消失，
            // 索引行状态与统计卡同步更新）
            this.clearSelection();
            await this.refreshData("none");
        },

        // 结果卡「重选失败项」：把清理失败文件的 path 重新勾选（仍在分组中时）
        reselectFailed() {
            if (!this.result) return;
            const failed = new Set((this.result.failedItems || []).map(f => f.path));
            if (!failed.size) return;
            for (const g of this.groups) {
                for (const it of g.files) {
                    if (failed.has(it.path)) g.checked.add(it.path);
                }
            }
            this.recomputeSummary();
        },

        // 结果卡「重新扫描」：清结果卡再扫描（scan 内部也会重置 result，双保险）
        rescan() {
            this.result = null;
            this.scan();
        },
    };
}
