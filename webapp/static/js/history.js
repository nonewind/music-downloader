// 下载页（Frontend 2.0 重写）：单页双段——上段活跃任务区 + 下段历史表格区
//
// 依赖全局工具（app.js 先于本脚本加载）：api()（统一 APP_BASE/401/业务码，
// 默认 15s timeout，本页全部走默认）、showToast()、uiConfirm()（Promise 确认框，
// 替代原生 confirm）、get_quality_name()、fmtDateTime()、fmtSizeBytes()、
// window.PLATFORM_META。渲染全部由 Alpine 模板绑定（x-for/x-text），本文件
// 不拼任何 innerHTML（x-text 输出天然免转义）。
//
// 轮询策略：任务轮询定时器常开（500ms），内部条件跳过——页面在后台
// （document.hidden）或任务区为空（hasActive=false）时不发请求，即
// "无任务静默、有任务 0.5s"，与 base.html 的全局 2s 状态栏轮询并存。
// 重试/重新下载会创建新任务，对应操作成功后立即 loadTasks() 刷新任务区
// 并唤醒轮询（全局轮询不回写本页状态，页面自身负责唤醒）。

function historyPage() {
    // 状态徽章映射（任务区与历史表共用；paused 为 0.7.0 新增：用户暂停）
    const STATUS_META = {
        success:     { text: "成功",   cls: "ui-badge-success" },
        done:        { text: "完成",   cls: "ui-badge-success" },
        failed:      { text: "失败",   cls: "ui-badge-danger" },
        skipped:     { text: "已下载", cls: "ui-badge-muted" },
        downloading: { text: "下载中", cls: "ui-badge-info" },
        pending:     { text: "等待中", cls: "ui-badge-warning" },
        paused:      { text: "已暂停", cls: "ui-badge-warning" },
    };

    return {
        // ---- 任务区状态 ----
        tasks: [],
        pausedAll: false,        // 服务端全局暂停标记（随每轮 /api/tasks 校正）
        hasActive: false,        // 任务区是否有任务（tasks.length>0），驱动轮询开关
        taskBusy: new Set(),     // 进行中的任务操作（"pk:action"），按钮防重
        allOpsBusy: false,       // 暂停全部/继续全部请求进行中
        pollTimer: null,         // 500ms 定时器（init 启动，常开、内部跳过）

        // ---- 历史区状态 ----
        songs: [],
        total: 0,
        page: 1,
        pages: 1,
        perPage: 20,
        statusFilter: "",
        keyword: "",
        loadingSongs: false,
        failedCount: 0,          // 库内失败记录总数（驱动失败重试栏显隐）
        checked: new Set(),      // 勾选行 pk（每次列表刷新后重置，与旧行为一致）
        deleting: null,          // 三选 Modal 目标：单删 {id,name,artists} / 批删 {ids:[pk]}
        failInfo: null,          // 失败原因弹窗内容 {name,artists,reason}
        redownloadBusy: null,    // 重新下载进行中的行 pk
        retryAllBusy: false,
        clearFailedBusy: false,

        // 防重入：上一轮请求未返回时跳过本轮（0.5s 轮询下必须）
        _loadingTasks: false,

        // Alpine 组件初始化
        async init() {
            await this.loadTasks();
            this.loadSongs();
            this.pollTimer = setInterval(() => {
                if (document.hidden || !this.hasActive) return;  // 后台/无任务静默
                this.loadTasks();
            }, 500);
        },

        // ============================================================
        // 活跃任务区
        // ============================================================
        statusMeta(status) {
            return STATUS_META[status] || { text: status, cls: "ui-badge-muted" };
        },

        // 任务操作防重：Set 存 "pk:action"，操作中的按钮 is-loading
        taskOpBusy(pk, action) {
            return this.taskBusy.has(pk + ":" + action);
        },
        // 该任务有任意操作进行中 → 整行按钮禁用（与旧版行级禁用一致）
        taskLocked(pk) {
            for (const k of this.taskBusy) {
                if (k.startsWith(pk + ":")) return true;
            }
            return false;
        },

        // 全局按钮可用性按列表重算（与旧版一致）：
        // 暂停全部：无"可暂停"任务时禁用；继续全部：有已暂停任务或处于
        // 全局暂停态时启用（便于清除全局标记）
        get canPauseAll() {
            return this.tasks.some(t => t.status !== "paused");
        },
        get canResumeAll() {
            return this.tasks.some(t => t.status === "paused") || this.pausedAll;
        },

        // 加载任务列表。Alpine 按 pk 键控 diff 渲染：服务端状态（暂停/继续、
        // 进度、错误信息）每轮校正徽章与按钮——包括被「暂停全部」或另一
        // 浏览器页签改变的情况
        async loadTasks() {
            if (this._loadingTasks) return;
            this._loadingTasks = true;
            try {
                const data = await api("/api/tasks");
                this.tasks = data.data || [];
                this.pausedAll = !!data.paused_all;
                this.hasActive = this.tasks.length > 0;
            } catch (e) {
                console.error("加载任务失败:", e);
            } finally {
                this._loadingTasks = false;
            }
        },

        // 单任务 暂停/继续/删除（action: pause|resume|delete；删除需确认）
        async taskAction(t, action) {
            if (this.taskLocked(t.pk)) return;
            if (action === "delete") {
                const ok = await uiConfirm({
                    title: "删除任务",
                    message: "确定删除该下载任务？\n正在下载的任务会立即停止，已下载的临时文件会被清理。",
                    confirmText: "删除",
                    danger: true,
                });
                if (!ok) return;
            }
            const key = t.pk + ":" + action;
            this.taskBusy.add(key);
            try {
                const data = action === "delete"
                    ? await api(`/api/tasks/${t.pk}`, { method: "DELETE" })
                    : await api(`/api/tasks/${t.pk}/${action}`, { method: "POST" });
                showToast(data.msg, "下载任务");
                await this.loadTasks();
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.taskBusy.delete(key);
            }
        },

        // 暂停全部：当前下载中的会立即停止，已下载部分保留（继续时断点续传）
        async pauseAll() {
            if (this.allOpsBusy) return;
            const ok = await uiConfirm({
                title: "暂停全部",
                message: "确定暂停全部下载任务？\n当前正在下载的任务会立即停止，已下载部分保留，继续时可断点续传。",
                confirmText: "暂停全部",
            });
            if (!ok) return;
            this.allOpsBusy = true;
            try {
                const data = await api("/api/tasks/pause-all", { method: "POST" });
                showToast(data.msg, "暂停");
                await this.loadTasks();     // 由 loadTasks 重算按钮可用性
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.allOpsBusy = false;
            }
        },

        // 继续全部（与旧语义一致：无确认）
        async resumeAll() {
            if (this.allOpsBusy) return;
            this.allOpsBusy = true;
            try {
                const data = await api("/api/tasks/resume-all", { method: "POST" });
                showToast(data.msg, "继续");
                await this.loadTasks();
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.allOpsBusy = false;
            }
        },

        // ============================================================
        // 下载历史
        // ============================================================
        async loadSongs(page) {
            if (page) this.page = page;
            if (this.loadingSongs) return;
            this.loadingSongs = true;
            try {
                const params = new URLSearchParams({ page: this.page, per_page: this.perPage });
                if (this.statusFilter) params.set("status", this.statusFilter);
                const kw = this.keyword.trim();
                if (kw) params.set("keyword", kw);
                const data = await api("/api/songs?" + params.toString());
                this.songs = data.data || [];
                this.total = data.total || 0;
                this.pages = data.pages || 1;
                // 勾选随刷新重置（与旧版每次加载后全不勾一致）
                this.checked = new Set();
                // 失败重试栏：只要库内存在失败记录就显示（不依赖当前筛选），
                // 让用户在任何视图下都能看到并处理失败记录
                try {
                    const failedData = await api("/api/songs?status=failed&per_page=1");
                    this.failedCount = failedData.total || 0;
                } catch (_) {
                    this.failedCount = 0;   // 查询失败不影响主列表，默认隐藏
                }
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.loadingSongs = false;
            }
        },

        // 查询（关键词回车 / 查询按钮共用；下拉与旧版一致不自动提交）
        search() {
            this.loadSongs(1);
        },

        // ---- 选择：全选与单选联动（半选态由模板 x-effect 驱动） ----
        get allChecked() {
            return this.songs.length > 0 && this.songs.every(s => this.checked.has(s.pk));
        },
        get checkedCount() {
            let n = 0;
            for (const s of this.songs) {
                if (this.checked.has(s.pk)) n++;
            }
            return n;
        },
        // 当前页勾选的 pk 数组（批量删除入参；pk 为 download_tasks 主键）
        get selectedPks() {
            return this.songs.filter(s => this.checked.has(s.pk)).map(s => s.pk);
        },
        toggleAll(on) {
            this.checked = on ? new Set(this.songs.map(s => s.pk)) : new Set();
        },
        toggleCheck(s, on) {
            if (on) this.checked.add(s.pk);
            else this.checked.delete(s.pk);
        },

        // ---- 分页：ui-pagination 组件类；窗口算法与 ui.pagination 宏同型：
        //      prev + 首页 + 前省略号(cur-3>2) + 当前±2 + 后省略号(cur+3<pages-1)
        //      + 末页 + next；pages<=1 不渲染 ----
        get pagerItems() {
            const cur = this.page, pages = this.pages;
            if (!pages || pages <= 1) return [];
            const items = [
                { type: "prev", p: cur - 1, label: "\u00ab" },
                { type: "num", p: 1, label: "1" },
            ];
            if (cur - 3 > 2) items.push({ type: "gap", label: "\u2026" });
            const start = Math.max(2, cur - 2);
            const end = Math.min(pages - 1, cur + 2);
            for (let i = start; i <= end; i++) items.push({ type: "num", p: i, label: String(i) });
            if (cur + 3 < pages - 1) items.push({ type: "gap", label: "\u2026" });
            items.push({ type: "num", p: pages, label: String(pages) });
            items.push({ type: "next", p: cur + 1, label: "\u00bb" });
            return items;
        },
        gotoPage(it) {
            if (it.type === "gap") return;
            if (it.type === "prev" && this.page <= 1) return;
            if (it.type === "next" && this.page >= this.pages) return;
            if (it.type === "num" && it.p === this.page) return;
            this.loadSongs(it.p);
        },

        // ---- 展示辅助 ----
        platName(s) {
            const meta = window.PLATFORM_META[s.platform];
            return (meta && meta.name) || s.platform_name || s.platform;
        },
        platBadgeCls(platform) {
            return window.PLATFORM_META[platform] ? "ui-badge-plat-" + platform : "ui-badge-muted";
        },
        // 服务端 downloaded_at 已是 "YYYY-MM-DD HH:MM:SS" 显示格式：直接展示
        //（绕开部分内核不解析空格日期的问题）；其余形状交给 fmtDateTime
        fmtSongTime(v) {
            if (typeof v === "string" && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(v)) return v;
            return fmtDateTime(v);
        },

        // ---- 失败原因弹窗 ----
        showFail(s) {
            this.failInfo = { name: s.name, artists: s.artists, reason: s.error_msg || "未知原因" };
            this.$store.modals.failReason = true;
        },

        // ---- 单条重试：song_id 字符串透传（QQ songmid 为非数字字符串，
        //      parseInt 会截断）；platform 一并提交（Song 为 (id, platform)
        //      复合主键，同 id 双平台并存时避免误重试） ----
        async retrySong(s) {
            try {
                const data = await api("/api/retry", {
                    method: "POST",
                    body: JSON.stringify({ song_ids: [String(s.id)], platform: s.platform || "" }),
                });
                showToast(data.msg, "重试");
                await this.loadTasks();     // 重试产生新任务：刷新任务区并唤醒轮询
                this.loadSongs(this.page);
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // ---- 重新下载：快照档位已知时明确告知原档位与降档保护；快照为空
        //      （存量记录）退回原文案。仅文案不同：请求参数不变（force 语义不变） ----
        async redownload(s) {
            if (this.redownloadBusy !== null) return;
            const qName = get_quality_name(s.quality || "");
            const ok = qName
                ? await uiConfirm({
                    title: "重新下载",
                    message: `该记录为「${qName}」音质，将按当前设置重新下载？\n若实际取得音质低于已有文件，任务将自动跳过保护。`,
                    confirmText: "重新下载",
                })
                : await uiConfirm({
                    title: "重新下载",
                    message: `《${s.name}》已下载过，重新下载？`,
                    confirmText: "重新下载",
                });
            if (!ok) return;
            this.redownloadBusy = s.pk;
            try {
                // fee 未知按 VIP 传：VIP 账号能下免费歌，按免费传则 VIP 歌会被卡
                const data = await api("/api/discover/download-song", {
                    method: "POST",
                    body: JSON.stringify({
                        song_id: String(s.id), name: s.name, artists: s.artists,
                        fee: 1, platform: s.platform || "", force: true,
                    }),
                });
                showToast(data.msg, "重新下载");
                await this.loadTasks();     // 重新下载产生新任务
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.redownloadBusy = null;
            }
        },

        // ---- 删除单条：成功记录 → 三选 Modal（可选是否连音乐文件一起删）；
        //      失败/跳过记录无有效音乐文件 → 直接确认删除 ----
        async deleteSongClick(s) {
            if (s.status === "success") {
                this.deleting = { id: s.pk, name: s.name, artists: s.artists };
                this.$store.modals.delSong = true;
                return;
            }
            const ok = await uiConfirm({
                title: "删除记录",
                message: "确定删除这条记录？",
                confirmText: "删除",
                danger: true,
            });
            if (ok) await this.doDeleteSong(s.pk, false);
        },

        // 删除下载记录（deleteFile=true 时同时删除该记录指向的本地音乐文件）
        async doDeleteSong(pk, deleteFile) {
            try {
                const url = deleteFile ? `/api/songs/${pk}?delete_file=1` : `/api/songs/${pk}`;
                const data = await api(url, { method: "DELETE" });
                showToast(data.msg || "已删除", "删除");
                await this.loadSongs(this.page);    // 刷新当前页
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // ---- 批量删除：含成功记录 → 三选 Modal；否则直接确认 ----
        async batchDeleteClick() {
            const pks = this.selectedPks;
            if (!pks.length) return;
            const hasSuccess = this.songs.some(s => this.checked.has(s.pk) && s.status === "success");
            if (hasSuccess) {
                this.deleting = { ids: pks };
                this.$store.modals.delSong = true;
                return;
            }
            const ok = await uiConfirm({
                title: "批量删除",
                message: `确定删除选中的 ${pks.length} 条记录？`,
                confirmText: "删除",
                danger: true,
            });
            if (ok) await this.doBatchDelete(pks, false);
        },

        async doBatchDelete(pks, deleteFile) {
            try {
                const data = await api("/api/songs/batch-delete", {
                    method: "POST",
                    body: JSON.stringify({ pks, delete_file: deleteFile }),
                });
                showToast(data.msg, "批量删除");
                await this.loadSongs(this.page);    // 刷新当前页
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // 三选 Modal 按钮（deleting 目标：{id} 单删 / {ids} 批删）
        async delSongRecordOnly() {
            const t = this.deleting;
            this.deleting = null;
            this.$store.modals.delSong = false;
            if (!t) return;
            if (t.ids) await this.doBatchDelete(t.ids, false);
            else await this.doDeleteSong(t.id, false);
        },
        async delSongWithFile() {
            const t = this.deleting;
            this.deleting = null;
            this.$store.modals.delSong = false;
            if (!t) return;
            if (t.ids) await this.doBatchDelete(t.ids, true);
            else await this.doDeleteSong(t.id, true);
        },

        // ---- 失败重试栏 ----
        async retryAll() {
            if (this.retryAllBusy) return;
            const ok = await uiConfirm({
                title: "全部重试",
                message: "确定重试所有失败歌曲？",
                confirmText: "重试",
            });
            if (!ok) return;
            this.retryAllBusy = true;
            try {
                const data = await api("/api/retry", { method: "POST", body: JSON.stringify({}) });
                showToast(data.msg, "重试");
                await this.loadTasks();     // 全部重试产生新任务
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.retryAllBusy = false;
            }
        },

        // 清除所有失败记录：重置筛选到默认并刷新
        async clearFailed() {
            if (this.clearFailedBusy) return;
            const ok = await uiConfirm({
                title: "清除失败记录",
                message: "确定清除所有失败记录？此操作不可恢复，清除后这些歌曲可重新下载。",
                confirmText: "清除",
                danger: true,
            });
            if (!ok) return;
            this.clearFailedBusy = true;
            try {
                const data = await api("/api/songs/failed", { method: "DELETE" });
                showToast(data.msg, "清除");
                this.statusFilter = "";
                this.keyword = "";
                this.page = 1;
                this.perPage = 20;
                await this.loadSongs();
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.clearFailedBusy = false;
            }
        },
    };
}
