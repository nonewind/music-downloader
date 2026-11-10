// 「我的歌单」列表（找歌页里的一块，嵌在 discoverPage 里面）
//
// 只管列表本身：自动同步开关 / 下载数量 / 同步 / 取关。
// 「立即同步」「用链接添加」两个按钮和添加弹窗归 discover.js。
// 两边靠窗口事件通气：
//   md:playlists-changed  找歌页添加了歌单 → 本列表重新加载
//   md:playlist-removed   本列表取关了歌单 → 找歌页刷新「已关注」标记
//
// 依赖全局工具（app.js 先于本脚本加载）：api()、showToast()、uiConfirm()、
// window.PLATFORM_META。
// 渲染全部由 Alpine 模板绑定（x-for/x-text），本文件不拼任何 innerHTML。
//
// API 契约：PUT /api/playlists/<pid> 带 body.platform（复合主键消歧）、
// DELETE /api/playlists/<pid>?platform=（query 传平台）、POST /api/sync/<pid>。

function playlistsPage() {
    return {
        playlists: [],
        loading: true,
        syncBusy: new Set(),   // 同步进行中的行 key（platform:id），按钮防重

        async init() {
            await this.loadPlaylists();
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

        // ---- 展示辅助 ----
        rowKey(p) {
            return p.platform + ":" + p.id;
        },
        platName(platform) {
            const meta = window.PLATFORM_META[platform];
            return (meta && meta.name) || platform;
        },
        // 第二行小字：平台 · 类型 · 共 N 首 · 上次同步时间
        plSub(p) {
            const parts = [];
            parts.push(p.platform_name || this.platName(p.platform));
            if (p.type === "official") {
                parts.push("官方榜单");
            } else {
                parts.push("自定义歌单");
            }
            parts.push("共 " + (p.track_count || 0) + " 首");
            if (p.last_synced_at) {
                parts.push("上次同步 " + p.last_synced_at);
            } else {
                parts.push("从未同步");
            }
            return parts.join(" · ");
        },

        // 自动同步开关（失败回滚列表）
        async toggleEnabled(p, on) {
            try {
                await api(`/api/playlists/${p.id}`, {
                    method: "PUT",
                    body: JSON.stringify({ enabled: on, platform: p.platform }),
                });
                p.enabled = on;
                showToast(on ? "已开启自动同步" : "已关闭自动同步");
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
                confirmText: "取消关注",
                danger: true,
            });
            if (!ok) return;
            try {
                await api(`/api/playlists/${p.id}?platform=${encodeURIComponent(p.platform)}`, {
                    method: "DELETE",
                });
                showToast("已取消关注");
                this.loadPlaylists();
                window.dispatchEvent(new CustomEvent("md:playlist-removed"));
            } catch (e) {
                showToast(e.message, "错误");
            }
        },
    };
}
