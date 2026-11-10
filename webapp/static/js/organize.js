// 音乐库页（原整理页）：Alpine 单一状态入口 organizePage()
//
// 两块内容：浏览（歌手 → 专辑 → 歌曲，按文件夹分组）和待处理（重复文件，
// 原整理页的重复组清理原样保留）。
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
//   GET  /api/library/tree        -> {artists:[{name,count,albums:[{dir,name,
//                                     year,count,size,lossless}]}],unplaced}
//   GET  /api/library/album?dir=  -> {dir,artist,name,year,albumartist,items:[...行 + track]}
//   POST /api/library/album/edit  -> preview=true：{new_dir,plans:[{from,to,moved,tags,skip}]}
//                                    preview=false：{new_dir,results:[{from,to,status,msg}]}
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
        scannedOnce: false,   // 已有可展示的扫描结果（区分「尚未扫描」与「无重复」空态）
        lastScan: null,       // 最近一次终态扫描任务（页头统计卡摘要）
        lastClean: null,      // 最近一次终态清理任务（页头统计卡摘要）
        index: { total: 0, active: 0, missing: 0 },  // 索引统计（state.index）
        result: null,         // 清理结果卡 {status,ok,trashed,failed,
                              //  failedItems,repaired,cleared}；null=不显示

        // ---- 音乐库浏览 ----
        view: "browse",       // browse=浏览 / pending=待处理
        artists: [],          // /api/library/tree 的 artists
        unplaced: 0,          // 不在「歌手/专辑/歌曲」三层结构里的文件数
        artistQuery: "",      // 歌手搜索框
        artist: null,         // 当前歌手（artists 里的一项）；null=没选
        album: null,          // 当前专辑 {dir,name,year,count,size,lossless,items}；null=没打开
        albumLoading: false,
        singlesOpen: false,   // 歌手页「单曲」段是否展开
        picked: new Set(),    // 勾选的文件路径（专辑歌曲 / 未归位文件）
        unplacedItems: [],    // /api/library/unplaced 的 items（带 problem）
        emptyDirs: [],        // /api/library/empty-dirs 的 dirs（相对路径）
        // 改专辑信息弹窗：stage=form 填表 / preview 看改动
        edit: { mode: "edit", stage: "form", fromDir: "", album: "", albumartist: "", year: "",
                moveFolder: true, paths: [], targetQuery: "", target: "", retag: true,
                busy: false, error: "", preview: null },

        pollTimer: null,      // 1s 轮询定时器（常开，内部条件跳过）
        _polling: false,      // 防重入：上一轮请求未返回时跳过本轮

        // Alpine 组件初始化：恢复服务端状态（在途任务/上次结果）
        async init() {
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
                await Promise.all([this.loadGroups("recommended"), this.loadTree(), this.loadPending()]);
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
            await Promise.all([this.loadGroups(preselect), this.loadTree(), this.loadPending()]);
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

        // ============================================================
        // 音乐库浏览（歌手 → 专辑 → 歌曲）
        // ============================================================
        async loadTree() {
            try {
                const data = await api("/api/library/tree");
                this.artists = data.data.artists;
                this.unplaced = data.data.unplaced;
            } catch (e) {
                showToast(e.message, "错误");
                return;
            }
            // 扫描/清理后重读：当前歌手、专辑按名字找回（可能已经没了）
            if (!this.artist) return;
            const name = this.artist.name;
            this.artist = this.artists.find(a => a.name === name) || null;
            if (!this.artist || !this.album) {
                this.album = null;
                return;
            }
            const dir = this.album.dir;
            const found = this.artist.albums.find(al => al.dir === dir);
            if (found) {
                await this.openAlbum(found);
            } else {
                this.album = null;
            }
        },

        // 手机上一次只显示一层；电脑上歌手栏一直在
        level() {
            if (this.album) return "album";
            if (this.artist) return "artist";
            return "artists";
        },

        get filteredArtists() {
            const q = this.artistQuery.trim().toLowerCase();
            if (!q) return this.artists;
            return this.artists.filter(a => a.name.toLowerCase().includes(q));
        },

        // 专辑文件夹里只有一首的算单曲，折叠成一段单独放
        get artistAlbums() {
            if (!this.artist) return [];
            return this.artist.albums.filter(al => al.count > 1);
        },
        get artistSingles() {
            if (!this.artist) return [];
            return this.artist.albums.filter(al => al.count === 1);
        },

        _scrollTopOnPhone() {
            if (window.matchMedia("(max-width: 767.98px)").matches) window.scrollTo(0, 0);
        },

        openArtist(a) {
            this.artist = a;
            this.album = null;
            this.clearPicked();
            // 只有单曲的歌手直接展开单曲段
            this.singlesOpen = this.artistAlbums.length === 0;
            this._scrollTopOnPhone();
        },

        async openAlbum(al) {
            this.album = { ...al, items: null };
            this.albumLoading = true;
            this.clearPicked();
            this._scrollTopOnPhone();
            try {
                const data = await api("/api/library/album?dir=" + encodeURIComponent(al.dir));
                // 加载期间又点了别的专辑：丢掉这次结果
                if (!this.album || this.album.dir !== al.dir) return;
                this.album.items = data.data.items;
                this.album.year = data.data.year;
                this.album.albumartist = data.data.albumartist;
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                if (this.album && this.album.dir === al.dir) this.albumLoading = false;
            }
        },

        backToArtists() {
            this.artist = null;
            this.album = null;
            this.clearPicked();
        },

        backToArtist() {
            this.album = null;
            this.clearPicked();
        },

        setView(v) {
            this.view = v;
            this.clearPicked();
        },

        // ============================================================
        // 勾选（专辑歌曲、未归位文件共用一份；换专辑/换页面时清空）
        // ============================================================
        togglePick(path, on) {
            const next = new Set(this.picked);
            if (on) next.add(path);
            else next.delete(path);
            this.picked = next;
        },

        clearPicked() {
            this.picked = new Set();
        },

        // 选中的歌移入回收站：复用重复文件清理的后台任务（进度条、结果卡都一样）
        async trashPicked() {
            if (this.cleaning || this.scanning || !this.picked.size) return;
            const ok = await uiConfirm({
                title: "移入回收站",
                message: "选中的 " + this.picked.size + " 首歌将移入下载目录下的 .trash 回收站，可手动恢复。",
                confirmText: "移入回收站",
                danger: true,
            });
            if (!ok) return;
            await this._submitClean([...this.picked], {});
        },

        // ============================================================
        // 待处理：未归位文件、空文件夹
        // ============================================================
        async loadPending() {
            try {
                const [un, empty] = await Promise.all([
                    api("/api/library/unplaced"),
                    api("/api/library/empty-dirs"),
                ]);
                this.unplacedItems = un.data.items;
                this.emptyDirs = empty.data.dirs;
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        pendingCount() {
            return this.groups.length + this.unplacedItems.length + this.emptyDirs.length;
        },

        async removeEmptyDirs() {
            const ok = await uiConfirm({
                title: "删掉空文件夹",
                message: "将删掉 " + this.emptyDirs.length + " 个空文件夹。只删一个文件都没有的文件夹，删的时候会再检查一遍。",
                confirmText: "删掉",
                danger: true,
            });
            if (!ok) return;
            try {
                const data = await api("/api/library/empty-dirs/remove", { method: "POST" });
                showToast(data.msg, "完成", "success");
            } catch (e) {
                showToast(e.message, "错误");
            }
            await this.loadPending();
        },

        // ============================================================
        // 改专辑信息 / 移到别的专辑（填表 → 预览 → 确认执行；
        // 预览和执行后端走同一份计划，两个弹窗共用 edit 状态和预览、页脚）
        // ============================================================
        _newEdit(mode) {
            return {
                mode,                 // edit=改专辑信息 / move=移到别的专辑
                stage: "form",
                fromDir: this.album ? this.album.dir : "",
                album: "", albumartist: "", year: "", moveFolder: true,
                paths: [], targetQuery: "", target: "", retag: true,
                busy: false, error: "", preview: null,
            };
        },

        openEdit() {
            this.edit = this._newEdit("edit");
            this.edit.album = this.album.name;
            this.edit.albumartist = this.album.albumartist;
            this.edit.year = this.album.year;
            this.$store.modals.albumEdit = true;
        },

        openMove() {
            if (!this.picked.size) return;
            this.edit = this._newEdit("move");
            this.edit.paths = [...this.picked];
            this.$store.modals.songMove = true;
        },

        closeChange() {
            this.$store.modals.albumEdit = false;
            this.$store.modals.songMove = false;
        },

        // 目标专辑候选：全部专辑（含单曲），按搜索词过滤，当前专辑除外，最多 50 个
        get moveTargets() {
            const q = this.edit.targetQuery.trim().toLowerCase();
            const out = [];
            for (const a of this.artists) {
                for (const al of a.albums) {
                    if (al.dir === this.edit.fromDir) continue;
                    const text = (a.name + " " + al.name).toLowerCase();
                    if (q && !text.includes(q)) continue;
                    out.push({ dir: al.dir, name: a.name + " / " + al.name, sub: this.albumSub(al) });
                    if (out.length >= 50) return out;
                }
            }
            return out;
        },

        // 只把改了的字段发给后端
        _editChanges() {
            const changes = {};
            const album = this.edit.album.trim();
            const albumartist = this.edit.albumartist.trim();
            const year = this.edit.year.trim();
            if (album !== this.album.name) changes.album = album;
            if (albumartist !== this.album.albumartist) changes.albumartist = albumartist;
            if (year !== this.album.year) changes.year = year;
            return changes;
        },

        async _postEdit(preview) {
            if (this.edit.mode === "move") {
                return api("/api/library/songs/move", {
                    method: "POST",
                    body: JSON.stringify({
                        paths: this.edit.paths,
                        target_dir: this.edit.target,
                        retag: this.edit.retag,
                        preview,
                    }),
                });
            }
            return api("/api/library/album/edit", {
                method: "POST",
                body: JSON.stringify({
                    dir: this.edit.fromDir,
                    changes: this._editChanges(),
                    move_folder: this.edit.moveFolder,
                    preview,
                }),
            });
        },

        async previewEdit() {
            if (this.edit.busy) return;
            this.edit.error = "";
            if (this.edit.mode === "move" && !this.edit.target) {
                this.edit.error = "先选一张目标专辑";
                return;
            }
            if (this.edit.mode === "edit" && !Object.keys(this._editChanges()).length) {
                this.edit.error = "没有改动任何内容";
                return;
            }
            this.edit.busy = true;
            try {
                const data = await this._postEdit(true);
                this.edit.preview = data.data;
                this.edit.stage = "preview";
            } catch (e) {
                this.edit.error = e.message;
            } finally {
                this.edit.busy = false;
            }
        },

        async applyEdit() {
            if (this.edit.busy) return;
            this.edit.busy = true;
            this.edit.error = "";
            let data;
            try {
                data = await this._postEdit(false);
            } catch (e) {
                this.edit.error = e.message;
                this.edit.busy = false;
                return;
            }
            this.edit.busy = false;
            this.closeChange();
            this.clearPicked();

            // 有失败的把第一条原因也带上
            const failed = data.data.results.filter(r => r.status === "failed");
            if (failed.length) {
                showToast(data.msg + "：" + failed[0].msg, "部分失败", "warning");
            } else {
                showToast(data.msg, "改好了", "success");
            }

            // 移动歌曲：留在原地，重读目录树（当前专辑还在就刷新，空了就回到歌手）和待处理
            if (this.edit.mode === "move") {
                await Promise.all([this.loadTree(), this.loadPending()]);
                return;
            }
            // 改专辑信息：重读目录树，打开改完后的专辑（文件夹可能换了名字、换了歌手）
            const newDir = data.data.new_dir;
            this.album = null;
            await this.loadTree();
            const artistName = newDir.split("/")[0];
            const artist = this.artists.find(a => a.name === artistName);
            if (!artist) return;
            this.openArtist(artist);
            const album = artist.albums.find(al => al.dir === newDir);
            if (album) await this.openAlbum(album);
        },

        previewCount(kind) {
            const plans = this.edit.preview.plans;
            if (kind === "skip") return plans.filter(p => p.skip).length;
            if (kind === "moved") return plans.filter(p => p.moved && !p.skip).length;
            return plans.filter(p => p.tags.length && !p.skip).length;
        },

        // 至少有一个文件真的会变，才允许确认
        previewDoable() {
            if (!this.edit.preview) return false;
            return this.edit.preview.plans.some(p => !p.skip && (p.moved || p.tags.length));
        },

        tagLabel(key) {
            return { album: "专辑", albumartist: "专辑歌手", year: "年份" }[key];
        },

        fileName(rel) {
            return rel.split("/").pop();
        },

        // ---- 文案 ----
        statsText() {
            if (!this.loaded) return "";
            if (!this.scannedOnce) return "扫描下载目录后，按歌手和专辑浏览、整理你的歌";
            let songs = 0;
            let bytes = 0;
            for (const a of this.artists) {
                songs += a.count;
                for (const al of a.albums) bytes += al.size;
            }
            const parts = [
                this.artists.length + " 位歌手",
                songs + " 首",
                fmtSizeBytes(bytes),
                "上次扫描 " + (this.lastScan.finished_at || this.lastScan.created_at),
            ];
            return parts.join(" · ");
        },

        artistSub(a) {
            const albums = a.albums.filter(al => al.count > 1).length;
            const singles = a.albums.length - albums;
            const parts = [];
            if (albums) parts.push(albums + " 张专辑");
            if (singles) parts.push(singles + " 首单曲");
            parts.push("共 " + a.count + " 首");
            return parts.join(" · ");
        },

        albumSub(al) {
            const parts = [];
            if (al.year) parts.push(al.year);
            parts.push(al.count + " 首");
            parts.push(al.lossless ? "无损" : "含有损");
            parts.push(fmtSizeBytes(al.size));
            return parts.join(" · ");
        },

        albumDetailSub() {
            const al = this.album;
            const parts = [this.artist.name];
            if (al.year) parts.push(al.year);
            parts.push(al.count + " 首");
            parts.push(al.lossless ? "全部无损" : "含有损");
            parts.push(fmtSizeBytes(al.size));
            return parts.join(" · ");
        },

        // ---- 上次扫描 / 最近清理 ----
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
            // 旧版直删模式的任务结果里还有 deleted，一起算进成功数
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
            const ok = await this._submitClean(
                this.selectedItems.map(it => it.path), this.buildRepair());
            if (ok) this.$store.modals.clean = false;
        },

        // 提交清理任务（重复文件清理、音乐库里删歌共用）；成功返回 true
        async _submitClean(paths, repair) {
            this.cleaning = true;    // 提交期间即锁定（防双击重复入队）
            try {
                // 提交即返回 job_id（默认 15s 超时足够），执行进度走轮询
                const data = await api("/api/organize/clean", {
                    method: "POST",
                    body: JSON.stringify({ delete_paths: paths, repair }),
                });
                const d = data.data || {};
                this.job = {
                    id: d.job_id, job_type: "clean", status: d.status,
                    progress: 0, processed: 0, total: paths.length,
                    error_msg: null, result: null,
                };
                this.startPolling();     // cleaning 保持 true，终态由 finishClean 复位
                return true;
            } catch (e) {
                this.cleaning = false;   // 提交失败：解锁可重试
                showToast(e.message, "错误");
                return false;
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
            const okCount = r.trashed || 0;
            this.result = {
                status: job.status,
                total: r.total ?? items.length,
                ok: okCount,
                failed: r.failed ?? failedItems.length,
                failedItems,
                repaired: r.repaired || [],
                cleared: r.cleared || [],
            };
            this.job = null;
            if (job.status === "success") {
                showToast("已把 " + okCount + " 个文件移入回收站", "清理完成", "success");
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
            this.clearPicked();
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
