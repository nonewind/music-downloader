// 总览页（Frontend 2.0 重写，UI-03e）：Alpine 单一状态入口 dashboardPage()
//
// 布局：顶部紧凑统计条（总下载/成功/失败/今日 + 关注歌单，数字+标签横排）
// + 主体两栏（左「账号状态」每平台一行的紧凑列表，右「快速操作」卡）。
// 下载任务的实时状态由全局底部状态栏（app.js）承载，本页不再单设任务区。
//
// 依赖全局工具（app.js 先于本脚本加载）：api()、showToast()。
// 渲染全部由 Alpine 模板绑定（x-for/x-text/:class/:style），本文件零 innerHTML。
//
// 功能与旧版逐项对齐：
//   GET  /api/stats          统计（30s 轮询，document.hidden 跳过）
//   GET  /api/accounts/stats 账号月额度（30s 轮询，document.hidden 跳过；
//                            VIP 徽章 / 月额度进度条 ≥80% 黄 ≥100% 红 / 不限额不显示进度条）
//   POST /api/sync-all       立即同步全部（按钮 busy 态）
// 轮询 timer 组件内统一管理：init() 启动、destroy() 清理（页面整页跳转
// 无需，SPA 式复用时防 timer 泄漏）。

function dashboardPage() {
    return {
        // ---- 唯一状态入口 ----
        stats: {
            total: null, success: null, failed: null, today: null,
            active_playlists: null, total_playlists: null,
        },
        statsLoaded: false,
        accounts: [],          // GET /api/accounts/stats（仅启用账号）
        accountsLoaded: false,
        syncing: false,        // 立即同步全部 busy 态
        _pollTimer: null,      // 统计 + 账号月额度共用轮询（同为 30s）

        // ============================================================
        // 初始化 / 销毁
        // ============================================================
        async init() {
            await Promise.all([this.loadStats(), this.loadAccountsStats()]);
            // 30s 轮询（页面在后台时不发无谓请求，切回前台下一轮自动恢复）
            this._pollTimer = setInterval(() => {
                if (document.hidden) return;
                this.loadStats();
                this.loadAccountsStats();
            }, 30000);
        },
        destroy() {
            if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
        },

        // ============================================================
        // 数据加载
        // ============================================================
        async loadStats() {
            try {
                const data = await api("/api/stats");
                this.stats = data.data || this.stats;
                this.statsLoaded = true;
            } catch (e) {
                console.error("加载统计失败:", e);
            }
        },

        async loadAccountsStats() {
            try {
                const data = await api("/api/accounts/stats");
                this.accounts = data.data || [];
                this.accountsLoaded = true;
            } catch (e) {
                console.error("加载账号状态失败:", e);
            }
        },

        // ============================================================
        // 操作
        // ============================================================
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

        // ============================================================
        // 月额度展示（进度条 ≥80% 黄 ≥100% 红；不限额不显示进度条）
        // ============================================================
        quotaText(a) {
            return a.unlimited
                ? `${a.monthly_downloaded} 首（不限）`
                : `${a.monthly_downloaded} / ${a.quota_limit} 首`;
        },
        quotaPct(a) {
            if (a.unlimited) return 0;
            return a.quota_limit > 0 ? Math.min(100, a.monthly_downloaded * 100 / a.quota_limit) : 0;
        },
        barStyle(a) {
            const pct = this.quotaPct(a);
            const color = pct >= 100 ? "var(--ui-danger)"
                : (pct >= 80 ? "var(--ui-warning)" : "var(--ui-success)");
            return `width:${pct}%;background:${color}`;
        },
    };
}
