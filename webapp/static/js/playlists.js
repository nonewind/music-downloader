// 歌单管理页（Frontend 2.0 重写，UI-03b）：纯歌单管理
//
// 旧页的「发现」（排行榜/热门歌单/搜索歌手）已拆分为独立 /discover 页，
// 本页只保留：我的歌单表格（启用开关/下载数量/同步/取关）+ 添加歌单弹窗。
//
// 依赖全局工具（app.js 先于本脚本加载）：api()、showToast()、uiConfirm()、
// window.PLATFORM_META、模板注入的 window._AVAILABLE_PLATFORMS。
// 渲染全部由 Alpine 模板绑定（x-for/x-text），本文件不拼任何 innerHTML。
//
// API 契约与旧页一致：PUT /api/playlists/<pid> 带 body.platform（复合主键
// 消歧）、DELETE /api/playlists/<pid>?platform=（query 传平台）、
// POST /api/sync/<pid>、POST /api/playlists {source,type,limit,platform}。

function playlistsPage() {
    return {
        // ---- 平台（添加歌单弹窗选择用，模板注入） ----
        platforms: window._AVAILABLE_PLATFORMS || [],

        // ---- 我的歌单 ----
        playlists: [],
        loading: true,
        syncBusy: new Set(),   // 同步进行中的行 key（platform:id），按钮防重

        // ---- 添加歌单弹窗 ----
        defaultLimit: 50,      // 添加歌单时的默认下载数量（来自 /api/settings）
        addForm: { source: "", platform: "netease", type: "user", limit: 50 },
        addBusy: false,

        async init() {
            await this.loadPlaylists();
            await this.loadDefaultLimit();
        },

        // 我的歌单列表
        async loadPlaylists() {
            this.loading = true;
            try {
                const data = await api("/api/playlists");
                this.playlists = data.data || [];
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.loading = false;
            }
        },

        // 加载默认下载数量（弹窗默认值；失败保持 50 兜底）
        async loadDefaultLimit() {
            try {
                const data = await api("/api/settings");
                const val = parseInt(data.data.default_playlist_limit);
                if (!isNaN(val) && val > 0) this.defaultLimit = val;
            } catch (e) {
                console.error("加载默认下载数量失败:", e);
            }
        },

        // ---- 展示辅助 ----
        rowKey(p) {
            return p.platform + ":" + p.id;
        },
        platName(platform) {
            const meta = window.PLATFORM_META[platform];
            return (meta && meta.name) || platform;
        },
        platBadgeCls(platform) {
            return window.PLATFORM_META[platform] ? "ui-badge-plat-" + platform : "ui-badge-muted";
        },
        typeText(p) {
            return p.type === "official" ? "官方榜单" : "自定义";
        },

        // 启用/禁用（失败回滚列表）
        async toggleEnabled(p, on) {
            try {
                await api(`/api/playlists/${p.id}`, {
                    method: "PUT",
                    body: JSON.stringify({ enabled: on, platform: p.platform }),
                });
                showToast(on ? "已启用" : "已禁用");
            } catch (e) {
                showToast(e.message, "错误");
                this.loadPlaylists();
            }
        },

        // 更新下载数量
        async updateLimit(p, raw) {
            try {
                await api(`/api/playlists/${p.id}`, {
                    method: "PUT",
                    body: JSON.stringify({ limit_count: parseInt(raw), platform: p.platform }),
                });
                showToast("已更新下载数量");
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // 立即同步单个歌单（行内 loading 态）
        async syncPlaylist(p) {
            const key = this.rowKey(p);
            if (this.syncBusy.has(key)) return;
            this.syncBusy = new Set([...this.syncBusy, key]);
            try {
                const data = await api(`/api/sync/${p.id}`, { method: "POST" });
                showToast(data.msg, "同步结果");
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                const next = new Set(this.syncBusy);
                next.delete(key);
                this.syncBusy = next;
            }
        },

        // 取消关注（已下载的歌曲不受影响；DELETE 以 query 传平台）
        async deletePlaylistClick(p) {
            const ok = await uiConfirm({
                title: "取消关注",
                message: `确定取消关注「${p.name}」吗？（已下载的歌曲不受影响）`,
                confirmText: "取关",
                danger: true,
            });
            if (!ok) return;
            try {
                await api(`/api/playlists/${p.id}?platform=${encodeURIComponent(p.platform)}`, {
                    method: "DELETE",
                });
                showToast("已删除");
                this.loadPlaylists();
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // ---- 添加歌单弹窗 ----
        openAdd() {
            this.addForm = { source: "", platform: "netease", type: "user", limit: this.defaultLimit };
            this.$store.modals.addPl = true;
        },

        async confirmAdd() {
            const f = this.addForm;
            if (!(f.source || "").trim()) { showToast("请输入歌单 ID 或链接"); return; }
            const limit = parseInt(f.limit) || this.defaultLimit;
            this.addBusy = true;
            try {
                const data = await api("/api/playlists", {
                    method: "POST",
                    body: JSON.stringify({ source: f.source.trim(), type: f.type, limit, platform: f.platform }),
                });
                showToast(data.msg, "添加成功");
                this.$store.modals.addPl = false;
                this.loadPlaylists();
            } catch (e) {
                showToast(e.message, "添加失败");
            } finally {
                this.addBusy = false;
            }
        },
    };
}
