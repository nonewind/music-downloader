// 音乐整理页（Frontend 2.0 重写）：Alpine 单一状态入口 organizePage()
//
// 依赖全局工具（app.js 先于本脚本加载）：api()（内部处理 APP_BASE 前缀与
// 401/403/code!=0）、showToast()、fmtSizeBytes()、fmtDurationMs()、fmtDate()。
// 渲染一律由 Alpine 模板绑定（x-for/x-text/:class），本文件不拼任何列表
// innerHTML（x-text 输出天然免转义）。
//
// 后端契约：
//   POST /api/organize/scan  -> {code:0,data:{scanned,duration_s,groups:[
//        {group_type:"same_id"|"identical",recommended,items:[...]}}]}
//   POST /api/organize/clean {delete_paths,repair:{被删:保留}}
//                            -> {code:0,data:{deleted,failed:[{path,reason}],
//                               repaired:[{song,from,to}]}}（timeout 300s）

function organizePage() {
    return {
        // ---- 唯一状态入口 ----
        scanning: false,
        scanned: 0,           // 最近一次扫描的音频文件总数
        duration: null,       // 扫描耗时（秒）
        groups: [],           // [{...后端组原样, files:[items], open, checked:Set(勾选path)}]
        selectedCount: 0,     // 勾选文件数（勾选变化时随 recomputeSummary 重算）
        selectedSongs: 0,     // 勾选涉及歌数（=有勾选的组数）
        freedBytes: 0,        // 勾选文件字节合计
        directDelete: null,   // true=永久删除模式；false=回收站模式；null=加载中（读自 /api/settings）
        scannedOnce: false,   // 区分「未扫描」与「扫描过但无重复」两种空态
        cleaning: false,
        result: null,         // 清理结果 {deleted,failed,repaired}；null=不显示结果卡

        // Alpine 组件初始化：读删除模式（页头徽章 + Modal 文案共用）
        async init() {
            try {
                const data = await api("/api/settings");
                this.directDelete = (data.data?.organize_direct_delete || "false") === "true";
            } catch (e) {
                this.directDelete = false;  // 读取失败按回收站模式兜底，不阻塞扫描
            }
        },

        // ============================================================
        // 扫描
        // ============================================================
        async scan() {
            if (this.scanning) return;  // 防重入：扫描期间忽略再次触发
            this.scanning = true;
            try {
                // 全量扫描解析音频元数据可能较慢，覆盖默认 15s 超时
                const data = await api("/api/organize/scan", { method: "POST", timeout: 300000 });
                const d = data.data || {};
                this.scanned = d.scanned || 0;
                this.duration = d.duration_s;
                // 每组展开：files 为后端 items；默认「按推荐选择」= 勾选除
                // recommended 外的全部文件（自由多选仍可后续改，含推荐）
                this.groups = (d.groups || []).map(g => ({
                    ...g,
                    files: g.items || [],
                    open: true,
                    checked: new Set((g.items || [])
                        .filter(it => it.path !== g.recommended)
                        .map(it => it.path)),
                }));
                this.result = null;      // 新扫描丢弃旧结果卡与旧清理计划
                this.scannedOnce = true;
                this.recomputeSummary();
            } catch (e) {
                // 403（仅管理员可访问）等：api() 已把后端 msg 转成 Error.message
                showToast(e.message, "错误");
            } finally {
                this.scanning = false;
            }
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
        // 组标题：same_id 显示 title - artist；identical 显示文件名
        songLabel(g) {
            const first = g.files[0] || {};
            if (g.group_type === "identical") return first.filename || "未知文件";
            return [first.title, first.artist].filter(Boolean).join(" - ")
                || first.filename || "未知歌曲";
        },

        // 规格列：无损 `FLAC 96kHz/24bit` / 有损 `MP3 320kbps`
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

        // ============================================================
        // 清理（Modal 流，Alpine store 'modals' 控制，不用原生 confirm）
        // ============================================================
        openCleanModal() {
            if (this.cleaning || !this.selectedCount) return;
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
            if (this.cleaning || !this.selectedCount) return;  // 防重复提交
            this.cleaning = true;
            try {
                const data = await api("/api/organize/clean", {
                    method: "POST",
                    body: JSON.stringify({
                        delete_paths: this.selectedItems.map(it => it.path),
                        repair: this.buildRepair(),
                    }),
                    timeout: 300000,
                });
                const d = data.data || {};
                this.applyCleanResult(d);
                this.$store.modals.clean = false;
                const failed = d.failed || [];
                if (!failed.length) {
                    showToast("已清理 " + (d.deleted || []).length + " 个文件", "清理完成", "success");
                } else {
                    showToast(failed.length + " 个文件清理失败，详见结果卡", "部分失败", "warning");
                }
            } catch (e) {
                // 请求异常：Modal 保持打开可重试，错误经 toast 提示
                showToast(e.message, "错误");
            } finally {
                this.cleaning = false;
            }
        },

        // 清理结果落账：从各组移除已删文件、剩余 <2 个文件的组视为已解决
        // 整组移除（不得残留旧清理计划）；再算汇总并展示结果卡
        applyCleanResult(d) {
            const deletedSet = new Set(d.deleted || []);
            this.groups = this.groups
                .map(g => {
                    const files = g.files.filter(it => !deletedSet.has(it.path));
                    return {
                        ...g,
                        files,
                        checked: new Set([...g.checked]
                            .filter(p => files.some(it => it.path === p))),
                    };
                })
                .filter(g => g.files.length >= 2);
            this.recomputeSummary();
            this.result = {
                deleted: d.deleted || [],
                failed: d.failed || [],
                repaired: d.repaired || [],
            };
        },

        // 结果卡「重新扫描」：清结果卡再扫描（scan 内部也会重置 result，双保险）
        rescan() {
            this.result = null;
            this.scan();
        },
    };
}
