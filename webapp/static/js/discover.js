// 找歌页（首页）：搜索 + 我的歌单 + 官方排行榜 + 热门歌单
//
// 布局：搜索框置顶（默认焦点）；搜索之后下面只显示结果（一行一首歌，
// 右边直接是状态：下载 / 排队中 / 下载中 62% / 已下载 / 失败可重试）；
// 没搜索时显示「我的歌单」（列表是独立组件 playlistsPage，见 playlists.js）、
// 排行榜、热门歌单（默认收起）。
//
// 依赖全局工具（app.js 先于本脚本加载）：api()、showToast()、uiConfirm()、
// get_quality_name()、get_current_quality_name()、fmtDate()、
// window.CACHED_SETTINGS、Alpine 的 queue 仓库（全局任务轮询结果）、
// 模板注入的 window._AVAILABLE_PLATFORMS。
// 渲染全部由 Alpine 模板绑定（x-for/x-text），本文件不拼任何 innerHTML。
//
// 平台状态记忆：按平台缓存一份筛选/页码/搜索词与已拉取数据，切平台时
// 快照保存/恢复；榜单/热门首次进入某平台才拉取，之后切回不再重复请求。

function discoverPage() {
    return {
        // ---- 平台 ----
        platforms: window._AVAILABLE_PLATFORMS || [],
        platform: (window._AVAILABLE_PLATFORMS && window._AVAILABLE_PLATFORMS[0])
            ? window._AVAILABLE_PLATFORMS[0].key : "netease",
        _platformState: {},   // 每平台快照（仅 JS 内部读写，模板不引用）

        // ---- 热门歌单是否展开（默认收起，首次展开才拉取；跨平台保持） ----
        hotOpen: false,

        // ---- 榜单（当前平台） ----
        toplists: [],
        toplistsLoaded: false,
        topLoading: false,

        // ---- 热门歌单（当前平台） ----
        cats: [],
        hotCat: "全部",
        hotOrder: "hot",
        hotLimit: 20,
        hotPage: 1,
        hotItems: [],
        hotTotal: 0,
        hotPages: 0,
        hotLoaded: false,
        hotLoading: false,

        // ---- 搜索（当前平台） ----
        search: {
            keyword: "", type: "song", limit: 50, page: 1,
            items: [], total: 0, pages: 0,
            qualities: {},      // downloaded_qualities：song_id(str) -> 音质档位（确认框文案用）
            searched: false,
        },
        searchLoading: false,
        allBusy: false,
        syncing: false,       // 「立即同步」全部歌单进行中

        // 队列活跃→空闲跃迁（app.js renderStatusbar 广播）：仅查本地库刷新
        // 当前页行状态（不重调平台搜索），让已入队歌曲尽快落到终态
        // （已下载 / 失败+原因徽标），失败原因不再只藏在下载历史页
        async _onQueueIdle() {
            const s = this.search;
            if (!s.searched || !s.items.length || this.searchLoading) return;
            try {
                const data = await api("/api/discover/song-flags", {
                    method: "POST",
                    body: JSON.stringify({
                        platform: this.platform,
                        ids: s.items.map(i => String(i.id)),
                    }),
                });
                const d = data.data || {};
                const dl = new Set(d.downloaded_ids || []);
                const act = new Set(d.active_ids || []);
                const failed = d.failed || {};
                s.qualities = Object.assign({}, s.qualities, d.downloaded_qualities || {});
                for (const it of s.items) {
                    const sid = String(it.id);
                    it.queued = act.has(sid);
                    it.downloading = act.has(sid);   // 与点击后的本地标记同语义
                    it.downloaded = dl.has(sid) || act.has(sid);
                    it.failed = Boolean(failed[sid]) && !act.has(sid);
                    it.fail_msg = failed[sid] || "";
                }
            } catch (_) {
                // 静默：行状态刷新属辅助信息，失败不打断页面
            }
        },

        // ---- 我的歌单 ID（发现卡片「已添加」标记） ----
        myPlaylistIds: new Set(),

        // ---- 添加歌单弹窗 ----
        defaultLimit: 50,
        addForm: { source: "", platform: "netease", type: "user", limit: 50 },
        addBusy: false,

        // ============================================================
        // 初始化
        // ============================================================
        async init() {
            // 搜索框默认焦点（搜索区置顶为主操作）
            this.$nextTick(() => { const el = this.$refs.kw; if (el) el.focus(); });
            await this.loadDefaultLimit();
            this.loadMyPlaylists();
            this.ensureSections();     // 默认展开的排行榜先拉
        },

        // 加载默认下载数量（添加歌单弹窗默认值），并缓存全局设置
        //（window.CACHED_SETTINGS：单首重复下载确认框取「当前设置音质」中文名）
        async loadDefaultLimit() {
            try {
                const data = await api("/api/settings");
                const val = parseInt(data.data.default_playlist_limit);
                if (!isNaN(val) && val > 0) this.defaultLimit = val;
                window.CACHED_SETTINGS = Object.assign({}, window.CACHED_SETTINGS, data.data || {});
            } catch (e) {
                console.error("加载默认下载数量失败:", e);
            }
        },

        // 我的歌单 ID 集合：发现卡片「已添加」置灰
        async loadMyPlaylists() {
            try {
                const data = await api("/api/playlists");
                this.myPlaylistIds = new Set((data.data || []).map(p => p.id));
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // ============================================================
        // 平台切换（快照保存/恢复 + 懒加载）
        // ============================================================
        switchPlatform(p) {
            if (p === this.platform) return;
            this._saveCurrentPlatformState();
            this.platform = p;
            this._restorePlatformState(p);
            this.ensureSections();
        },

        _saveCurrentPlatformState() {
            this._platformState[this.platform] = {
                toplists: this.toplists, toplistsLoaded: this.toplistsLoaded,
                cats: this.cats,
                hotCat: this.hotCat, hotOrder: this.hotOrder, hotLimit: this.hotLimit,
                hotPage: this.hotPage, hotItems: this.hotItems,
                hotTotal: this.hotTotal, hotPages: this.hotPages, hotLoaded: this.hotLoaded,
                search: { ...this.search },
            };
        },

        _restorePlatformState(p) {
            const snap = this._platformState[p];
            if (snap) {
                this.toplists = snap.toplists;
                this.toplistsLoaded = snap.toplistsLoaded;
                this.cats = snap.cats;
                this.hotCat = snap.hotCat;
                this.hotOrder = snap.hotOrder;
                this.hotLimit = snap.hotLimit;
                this.hotPage = snap.hotPage;
                this.hotItems = snap.hotItems;
                this.hotTotal = snap.hotTotal;
                this.hotPages = snap.hotPages;
                this.hotLoaded = snap.hotLoaded;
                this.search = { ...snap.search };
            } else {
                // 新平台：恢复默认筛选，数据进入分区时才拉（懒加载标记复位）
                this.toplists = [];
                this.toplistsLoaded = false;
                this.cats = [];
                this.hotCat = "全部";
                this.hotOrder = "hot";
                this.hotLimit = 20;
                this.hotPage = 1;
                this.hotItems = [];
                this.hotTotal = 0;
                this.hotPages = 0;
                this.hotLoaded = false;
                this.search = {
                    keyword: "", type: "song", limit: 50, page: 1,
                    items: [], total: 0, pages: 0, qualities: {}, searched: false,
                };
            }
        },

        // 折叠分区懒加载：展开且该平台未拉过才请求
        ensureSections() {
            if (!this.toplistsLoaded && !this.topLoading) this.loadToplists();
            if (this.hotOpen && !this.hotLoaded && !this.hotLoading) {
                this.loadCategories();
                this.loadHot(1);
            }
        },

        toggleHot() {
            this.hotOpen = !this.hotOpen;
            if (this.hotOpen) this.ensureSections();
        },

        // ============================================================
        // 官方排行榜
        // ============================================================
        async loadToplists() {
            this.topLoading = true;
            try {
                const data = await api("/api/discover/toplists?platform=" + this.platform);
                this.toplists = data.data || [];
                this.toplistsLoaded = true;
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.topLoading = false;
            }
        },

        // ============================================================
        // 热门歌单
        // ============================================================
        async loadCategories() {
            try {
                const data = await api("/api/discover/categories?platform=" + this.platform);
                this.cats = data.data || [];
            } catch (e) {
                console.error("加载分类失败:", e);
            }
        },

        async loadHot(page) {
            if (page) this.hotPage = page;
            this.hotLoading = true;
            try {
                const params = new URLSearchParams({
                    cat: this.hotCat, limit: this.hotLimit,
                    order: this.hotOrder, page: this.hotPage, platform: this.platform,
                });
                const data = await api("/api/discover/playlists?" + params.toString());
                this.hotItems = data.data || [];
                this.hotTotal = data.total || 0;
                this.hotPages = data.pages || 0;
                this.hotPage = data.page || this.hotPage;   // 同步后端返回的实际页码
                this.hotLoaded = true;
            } catch (e) {
                showToast(e.message, "错误");
                this.hotItems = [];
                this.hotTotal = 0;
                this.hotPages = 0;
            } finally {
                this.hotLoading = false;
            }
        },

        get hotTitle() {
            return (this.hotCat === "全部" ? "热门歌单" : this.hotCat + "歌单")
                + "（" + (this.hotOrder === "hot" ? "热门" : "最新") + "）";
        },

        // ============================================================
        // 搜索（歌曲/歌手 → 歌曲表；专辑 → 专辑表）
        // ============================================================
        get searchIsAlbum() {
            return this.search.type === "album";
        },

        // 点「歌曲 / 歌手 / 专辑」：换类型，输入框里有词就直接重新搜
        setType(type) {
            if (type === this.search.type) return;
            this.search.type = type;
            this.resetSearchOnType();
            if ((this.search.keyword || "").trim()) this.doSearch();
        },

        // 点输入框里的 ×：清空搜索，回到「我的歌单 / 排行榜」
        clearSearch() {
            this.search.keyword = "";
            this.resetSearchOnType();
            this.$nextTick(() => { const el = this.$refs.kw; if (el) el.focus(); });
        },

        // 重置页码并清空结果
        resetSearchOnType() {
            this.search.page = 1;
            this.search.searched = false;
            this.search.items = [];
            this.search.total = 0;
            this.search.pages = 0;
        },

        doSearch() {
            const kw = (this.search.keyword || "").trim();
            if (!kw) { showToast("请输入搜索关键词"); return; }
            this.search.keyword = kw;
            this.search.page = 1;
            this.loadSearch();
        },

        async loadSearch() {
            const s = this.search;
            this.searchLoading = true;
            try {
                const data = await api("/api/discover/search", {
                    method: "POST",
                    body: JSON.stringify({
                        keyword: s.keyword, type: s.type, limit: s.limit,
                        offset: (s.page - 1) * s.limit, platform: this.platform,
                    }),
                });
                const d = data.data || {};
                s.items = d.items || [];
                s.total = d.total || 0;
                s.pages = d.pages || 0;
                s.page = d.page || s.page;      // 同步后端返回的实际页码
                s.qualities = d.downloaded_qualities || {};
                s.searched = true;
            } catch (e) {
                showToast(e.message, "错误");
                s.items = [];
                s.total = 0;
                s.pages = 0;
            } finally {
                this.searchLoading = false;
            }
        },

        // ============================================================
        // 搜索结果行的状态（模板据此决定右边显示什么）
        // ============================================================
        // 这首歌在下载队列里的任务（没有则为 null）
        rowTask(s) {
            const sid = String(s.id);
            const tasks = this.$store.queue.tasks;
            for (const t of tasks) {
                if (String(t.song_id) === sid && t.platform === this.platform) return t;
            }
            return null;
        },

        // busy 请求中 / downloading 下载中 / pending 排队中 / paused 已暂停 /
        // failed 失败 / done 已下载 / none 还没下载
        rowState(s) {
            if (s.busy) return "busy";
            const task = this.rowTask(s);
            if (task) {
                if (task.status === "downloading") return "downloading";
                if (task.status === "paused") return "paused";
                return "pending";
            }
            // 刚点完下载、轮询还没带回任务的那一两秒
            if (s.downloading || s.queued) return "pending";
            if (s.failed) return "failed";
            if (s.downloaded) return "done";
            return "none";
        },

        rowPct(s) {
            const task = this.rowTask(s);
            if (!task) return 0;
            return Math.max(0, Math.min(100, task.progress || 0));
        },

        // 专辑行第二行：歌手 · N 首 · 发行日期
        albumSub(a) {
            const parts = [];
            if (a.artist) parts.push(a.artist);
            if (a.size) parts.push(a.size + " 首");
            if (a.publish_time) parts.push(fmtDate(a.publish_time));
            return parts.join(" · ");
        },

        // ============================================================
        // 下载：单首 / 全部下载（当前页）/ 专辑
        // ============================================================

        // 单首下载（不受排除过滤限制）。已下载歌曲先二次确认（双文案：
        // 已有档位取 downloaded_qualities 角标，当前设置档位取全局设置缓存；
        // 未知则省略括号），确认后 force=true 重新入队。请求参数与旧页一致。
        async downloadSong(s) {
            let force = false;
            if (s.downloaded) {
                const existQ = get_quality_name(this.search.qualities[String(s.id)] || "");
                const curQ = get_current_quality_name(this.platform);
                const msg = `《${s.name}》已下载过${existQ ? `（${existQ}）` : ""}。` +
                    `将${curQ ? `以当前设置音质（${curQ}）` : ""}重新下载？\n` +
                    "若实际取得音质低于已有文件，任务将自动跳过保护。";
                const ok = await uiConfirm({ title: "重新下载", message: msg, confirmText: "重新下载" });
                if (!ok) return;
                force = true;
            }
            s.busy = true;
            try {
                const data = await api("/api/discover/download-song", {
                    method: "POST",
                    body: JSON.stringify({
                        song_id: String(s.id), name: s.name, artists: s.artists,
                        fee: s.fee || 0, platform: this.platform, force,
                    }),
                });
                showToast(data.msg, "下载");
                s.downloaded = true;
                s.downloading = true;
                // 快速失败兜底：状态栏 2s 轮询可能整段错过短命任务
                // （入队后秒失败，队列从未在采样点呈活跃态），延迟单次
                // 刷新让失败徽标/终态尽快可见；长任务由 md:queue-idle
                // 跃迁（app.js renderStatusbar 广播）覆盖
                setTimeout(() => this._onQueueIdle(), 2500);
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                s.busy = false;
            }
        },

        // 全部下载当前页（应用排除关键字过滤，仅歌曲模式显示按钮）
        async downloadAllClick() {
            const kw = this.search.keyword;
            if (!kw) { showToast("请先输入搜索关键词"); return; }
            const ok = await uiConfirm({
                title: "全部下载",
                message: `确定下载「${kw}」当前页（第 ${this.search.page} 页）的全部歌曲吗？\n（将应用排除关键字过滤）`,
                confirmText: "全部下载",
            });
            if (!ok) return;
            this.allBusy = true;
            try {
                const data = await api("/api/discover/search-download", {
                    method: "POST",
                    body: JSON.stringify({
                        keyword: kw, limit: this.search.limit,
                        offset: (this.search.page - 1) * this.search.limit,
                        platform: this.platform,
                    }),
                });
                showToast(data.msg, "下载结果");
                await this.loadSearch();    // 下载后刷新当前页已下载状态
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.allBusy = false;
            }
        },

        // 专辑下载（下载整张专辑，应用排除关键字过滤）
        async downloadAlbum(a) {
            const ok = await uiConfirm({
                title: "下载专辑",
                message: `确定下载专辑「${a.name}」内的全部歌曲吗？\n（将应用排除关键字过滤）`,
                confirmText: "下载",
            });
            if (!ok) return;
            a.busy = true;
            try {
                const data = await api("/api/discover/album-download", {
                    method: "POST",
                    body: JSON.stringify({
                        album_id: String(a.id), album_name: a.name, platform: this.platform,
                    }),
                });
                showToast(data.msg, "专辑下载");
                a.queued = true;
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                a.busy = false;
            }
        },

        // ============================================================
        // 发现卡片：添加歌单（榜单 official / 热门 user）
        // ============================================================
        isAdded(id) {
            return this.myPlaylistIds.has(id) || this.myPlaylistIds.has(parseInt(id));
        },

        // 非歌曲榜（后端 get_toplists 返回 addable=false）置灰；严格等判定
        // 保证无该字段的平台/热门歌单不受影响
        cardBtnCls(item) {
            return (this.isAdded(item.id) || item.addable === false) ? "ui-btn-secondary" : "ui-btn-primary";
        },

        cardMeta(item, source) {
            if (source === "toplist") {
                return item.addable === false ? "非歌曲榜" : (item.update_frequency || "排行榜");
            }
            const pc = item.play_count ? this.formatPlayCount(item.play_count) : "";
            return pc ? "播放 " + pc : "歌单";
        },

        formatPlayCount(n) {
            if (n >= 100000000) return (n / 100000000).toFixed(1) + "亿";
            if (n >= 10000) return (n / 10000).toFixed(1) + "万";
            return String(n);
        },

        async addFromCard(item, type) {
            if (this.isAdded(item.id) || item.addable === false || item.busy) return;
            item.busy = true;
            try {
                const data = await this.addPlaylist(item.id, type, this.defaultLimit, this.platform);
                showToast(data.msg, "添加成功");
                this.myPlaylistIds = new Set([...this.myPlaylistIds, parseInt(item.id)]);
                window.dispatchEvent(new CustomEvent("md:playlists-changed"));
            } catch (e) {
                showToast(e.message, "添加失败");
            } finally {
                item.busy = false;
            }
        },

        // 立即同步全部已启用的歌单
        async syncAll() {
            if (this.syncing) return;
            this.syncing = true;
            try {
                const data = await api("/api/sync-all", { method: "POST" });
                showToast(data.msg, "同步结果", "success");
            } catch (e) {
                showToast(e.message, "错误", "error");
            } finally {
                this.syncing = false;
            }
        },

        addPlaylist(source, type, limit, platform) {
            return api("/api/playlists", {
                method: "POST",
                body: JSON.stringify({ source, type, limit, platform }),
            });
        },

        // ============================================================
        // 添加歌单弹窗（ID/链接 + 平台 + 类型 + 默认前 N）
        // ============================================================
        openAdd() {
            this.addForm = {
                source: "", platform: this.platform,
                type: "user", limit: this.defaultLimit,
            };
            this.$store.modals.addPl = true;
        },

        async confirmAdd() {
            const f = this.addForm;
            if (!(f.source || "").trim()) { showToast("请输入歌单 ID 或链接"); return; }
            const limit = parseInt(f.limit) || this.defaultLimit;
            this.addBusy = true;
            try {
                const data = await this.addPlaylist(f.source.trim(), f.type, limit, f.platform);
                showToast(data.msg, "添加成功");
                this.$store.modals.addPl = false;
                this.loadMyPlaylists();     // 新歌单可能就是发现卡片里的榜单
                window.dispatchEvent(new CustomEvent("md:playlists-changed"));
            } catch (e) {
                showToast(e.message, "添加失败");
            } finally {
                this.addBusy = false;
            }
        },

        // ============================================================
        // 分页：ui-pagination 组件类；窗口算法与 history 页同型
        //      prev + 首页 + 前省略号 + 当前±2 + 后省略号 + 末页 + next
        // ============================================================
        pager(cur, pages) {
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
        get hotPagerItems() { return this.pager(this.hotPage, this.hotPages); },
        get searchPagerItems() { return this.pager(this.search.page, this.search.pages); },

        gotoHot(it) {
            if (it.type === "gap") return;
            if (it.type === "prev" && this.hotPage <= 1) return;
            if (it.type === "next" && this.hotPage >= this.hotPages) return;
            if (it.type === "num" && it.p === this.hotPage) return;
            this.loadHot(it.p);
        },
        gotoSearch(it) {
            if (it.type === "gap") return;
            if (it.type === "prev" && this.search.page <= 1) return;
            if (it.type === "next" && this.search.page >= this.search.pages) return;
            if (it.type === "num" && it.p === this.search.page) return;
            this.search.page = it.p;
            this.loadSearch();
        },

    };
}
