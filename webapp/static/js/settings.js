// 设置页（Frontend 2.0 重写，UI-03d）：Alpine 单一状态入口 settingsPage()
//
// 依赖全局工具（app.js 先于本脚本加载）：api()（APP_BASE 前缀 / 401、403 /
// code!=0 统一处理，options.timeout 可覆盖默认 15s）、showToast()、uiConfirm()。
// 渲染全部由 Alpine 模板绑定（x-model / x-text / :class），本文件零 innerHTML。
//
// 后端契约（与旧页逐字段对齐）：
//   GET    /api/settings                  全量配置（~34 项字符串键值；level_<p> 为空时
//                                         后端已回填旧全局 level；exclude_scope 兼容旧值
//                                         "both" = 歌单同步+搜索下载）
//   PUT    /api/settings                  全量提交（对齐现状：34 键全集，非增量；仅管理员
//                                         ——window.IS_ADMIN 前端禁用保存，后端二次校验；
//                                         端口钳制/档位重置/重启提示由后端拼进 msg 经 toast 透传）
//   GET    /api/settings/migrate-layout   {needs} 目录结构待迁移数量（needs>0 显示提示条）
//   POST   /api/settings/migrate-layout   执行迁移（移动用户文件 → uiConfirm 显式确认；
//                                         文件较多耗时较长 → timeout 120s）
//   GET    /api/{ncm|qq|kugou}/status     内置 API 服务状态（timeout 5s；失败保留上次状态）
//   POST   /api/{ncm|qq|kugou}/start      启动（首次自解压最长约 60s → timeout 70s）
//   POST   /api/{ncm|qq|kugou}/stop       停止（危险操作 → uiConfirm danger 确认）
//
// 三服务状态轮询组件内统一管理：唯一 timer _pollTimer（3s，document.hidden
// 跳过），init() 启动、destroy() 清理；自定义 URL 勾选时内置区块锁定且启停
// 操作拦截提示；服务运行中禁改端口（title 提示先停止服务）。

// 三平台内置 API 服务描述（模板 x-for 的唯一数据源；key 同时是 API 前缀）
const SERVICE_DEFS = [
    {
        key: "ncm", name: "网易云API服务",
        portKey: "ncm_api_port", autoKey: "ncm_api_auto_start",
        customKey: "use_custom_api_url", urlKey: "custom_api_url",
        urlPlaceholder: "http://127.0.0.1:3000",
    },
    {
        key: "qq", name: "QQ音乐API服务",
        portKey: "qq_api_port", autoKey: "qq_api_auto_start",
        customKey: "use_custom_qq_api_url", urlKey: "qq_api_base_url",
        urlPlaceholder: "http://127.0.0.1:45602",
    },
    {
        key: "kugou", name: "酷狗音乐API服务",
        portKey: "kugou_api_port", autoKey: "kugou_api_auto_start",
        customKey: "use_custom_kugou_api_url", urlKey: "kugou_api_base_url",
        urlPlaceholder: "http://127.0.0.1:45603",
    },
];

function svcDef(key) {
    return SERVICE_DEFS.find(d => d.key === key);
}

// 单个服务的运行时状态（每次调用 settingsPage() 随实例新建，无跨页共享）
const blankSvcState = () => ({ data: null, starting: false, stopping: false });

function settingsPage() {
    return {
        // ---- 唯一状态入口 ----
        serviceList: SERVICE_DEFS,
        form: {},                 // loadSettings() 全量回填（含 organize_direct_delete）
        loading: true,
        saving: false,
        // 目录结构迁移：needs=null 检测中/隐藏提示条；>0 显示提示条
        migrate: { needs: null, busy: false },
        services: { ncm: blankSvcState(), qq: blankSvcState(), kugou: blankSvcState() },
        _pollTimer: null,         // 三服务状态轮询（组件内唯一 timer）

        // 保存按钮可用性：window.IS_ADMIN 由模板注入（后端 PUT 另有二次校验）
        get isAdmin() { return window.IS_ADMIN === true; },

        // ============================================================
        // 生命周期
        // ============================================================
        async init() {
            this.refreshAllSvc();
            // 3s 轮询三服务状态（页面在后台时不发无谓请求，切回前台下一轮恢复）
            this._pollTimer = setInterval(() => this.pollSvcStatuses(), 3000);
            await this.loadSettings();
            this.refreshMigrateHint();
        },
        // 组件销毁兜底清理（页面整页跳转无需，SPA 式复用时防 timer 泄漏）
        destroy() {
            if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
        },

        // ============================================================
        // 配置回填与保存
        // ============================================================
        async loadSettings() {
            try {
                const data = await api("/api/settings");
                this.applySettings(data.data || {});
            } catch (e) {
                showToast(e.message, "错误", "error");
            } finally {
                this.loading = false;
            }
        },

        // GET data → 表单状态（与旧页逐字段同语义；新增 organize_direct_delete）
        applySettings(s) {
            // exclude_scope 兼容旧值："both" = 歌单同步 + 搜索下载
            const rawScope = s.exclude_scope || "playlist,search";
            const scopes = rawScope === "both"
                ? ["playlist", "search"]
                : String(rawScope).split(",").map(x => x.trim()).filter(Boolean);
            this.form = {
                // Web / API 服务
                web_port: s.web_port || "*:45600",
                ncm_api_port: s.ncm_api_port || "45601",
                ncm_api_auto_start: s.ncm_api_auto_start === "true",
                use_custom_api_url: s.use_custom_api_url === "true",
                custom_api_url: s.custom_api_url || "",
                qq_api_port: s.qq_api_port || "45602",
                qq_api_auto_start: s.qq_api_auto_start === "true",
                use_custom_qq_api_url: s.use_custom_qq_api_url === "true",
                qq_api_base_url: s.qq_api_base_url || "http://127.0.0.1:45602",
                kugou_api_port: s.kugou_api_port || "45603",
                kugou_api_auto_start: s.kugou_api_auto_start === "true",
                use_custom_kugou_api_url: s.use_custom_kugou_api_url === "true",
                kugou_api_base_url: s.kugou_api_base_url || "http://127.0.0.1:45603",
                // 下载行为
                output_dir: s.output_dir || "",
                write_metadata: s.write_metadata === "true",
                write_lyric: s.write_lyric === "true",
                filename_include_album: s.filename_include_album !== "false",
                overwrite_existing: s.overwrite_existing === "true",
                max_retries: s.max_retries || "3",
                default_playlist_limit: s.default_playlist_limit || "50",
                exclude_keywords: s.exclude_keywords || "",
                scope_playlist: scopes.includes("playlist"),
                scope_search: scopes.includes("search"),
                organize_direct_delete: s.organize_direct_delete === "true",
                // 目录结构
                dir_layout: s.dir_layout || "artist_album",
                // 音质（后端未单独设置 level_<p> 时已回填旧全局 level）
                level_netease: s.level_netease || "exhigh",
                level_qq: s.level_qq || "exhigh",
                level_kugou: s.level_kugou || "exhigh",
                enable_quality_fallback: s.enable_quality_fallback !== "false",
                // 多账号
                download_mode: s.download_mode || "fallback",
                prefer_non_vip: s.prefer_non_vip === "true",
                hourly_limit_per_account: s.hourly_limit_per_account || "50",
                // 自动同步
                auto_sync_enabled: s.auto_sync_enabled === "true",
                sync_times: s.sync_times || "03:00,09:00,21:00",
                sync_jitter: s.sync_jitter || "600",
            };
        },

        // 表单 → 全量 payload（34 键 = 后端 DEFAULT_SETTINGS 全集，非增量）。
        // 布尔统一 "true"/"false" 字符串；自定义 URL 未勾选时提交空串（与旧页一致）
        buildPayload() {
            const f = this.form;
            const flag = (v) => (v ? "true" : "false");
            const txt = (v) => String(v ?? "").trim();
            const scope = [];
            if (f.scope_playlist) scope.push("playlist");
            if (f.scope_search) scope.push("search");
            return {
                web_port: txt(f.web_port),
                ncm_api_port: txt(f.ncm_api_port),
                ncm_api_auto_start: flag(f.ncm_api_auto_start),
                use_custom_api_url: flag(f.use_custom_api_url),
                custom_api_url: f.use_custom_api_url ? txt(f.custom_api_url) : "",
                qq_api_port: txt(f.qq_api_port),
                qq_api_auto_start: flag(f.qq_api_auto_start),
                use_custom_qq_api_url: flag(f.use_custom_qq_api_url),
                qq_api_base_url: f.use_custom_qq_api_url ? txt(f.qq_api_base_url) : "",
                kugou_api_port: txt(f.kugou_api_port),
                kugou_api_auto_start: flag(f.kugou_api_auto_start),
                use_custom_kugou_api_url: flag(f.use_custom_kugou_api_url),
                kugou_api_base_url: f.use_custom_kugou_api_url ? txt(f.kugou_api_base_url) : "",
                output_dir: txt(f.output_dir),
                write_metadata: flag(f.write_metadata),
                write_lyric: flag(f.write_lyric),
                filename_include_album: flag(f.filename_include_album),
                overwrite_existing: flag(f.overwrite_existing),
                max_retries: txt(f.max_retries),
                default_playlist_limit: txt(f.default_playlist_limit),
                exclude_keywords: txt(f.exclude_keywords),
                exclude_scope: scope.join(","),
                organize_direct_delete: flag(f.organize_direct_delete),
                dir_layout: f.dir_layout,
                level_netease: f.level_netease,
                level_qq: f.level_qq,
                level_kugou: f.level_kugou,
                enable_quality_fallback: flag(f.enable_quality_fallback),
                download_mode: f.download_mode,
                prefer_non_vip: flag(f.prefer_non_vip),
                hourly_limit_per_account: txt(f.hourly_limit_per_account),
                auto_sync_enabled: flag(f.auto_sync_enabled),
                sync_times: txt(f.sync_times),
                sync_jitter: txt(f.sync_jitter),
            };
        },

        async save() {
            if (this.saving || this.loading) return;   // 回填未完成不提交默认值
            this.saving = true;
            try {
                // 钳制警告/非法档位重置/Web 端口重启提示由后端拼进 msg，toast 原样透传
                const data = await api("/api/settings", {
                    method: "PUT",
                    body: JSON.stringify(this.buildPayload()),
                });
                showToast(data.msg, "成功", "success");
                await this.loadSettings();
                this.refreshAllSvc();   // 端口修改触发自动重启后立即刷新状态显示
            } catch (e) {
                showToast(e.message, "错误", "error");
            } finally {
                this.saving = false;
            }
        },

        // ============================================================
        // 目录结构存量迁移
        // ============================================================
        async refreshMigrateHint() {
            try {
                const data = await api("/api/settings/migrate-layout");
                this.migrate.needs = (data.data && data.data.needs) || 0;
            } catch (e) {
                this.migrate.needs = 0;   // 检测失败不打扰用户
            }
        },

        // 手动触发迁移（移动用户文件必须显式确认，不做自动迁移；幂等可重复）
        async runMigrate() {
            if (this.migrate.busy) return;
            const ok = await uiConfirm({
                title: "迁移到「歌手 / 专辑」结构",
                message: "将把已下载的存量文件搬入「歌手 / 专辑」目录。只搬位置不改文件名，已在原地的自动跳过；文件较多时可能耗时较长。",
                confirmText: "开始迁移",
            });
            if (!ok) return;
            this.migrate.busy = true;
            try {
                const data = await api("/api/settings/migrate-layout", { method: "POST", timeout: 120000 });
                showToast(data.msg, "迁移", "success");
                this.refreshMigrateHint();   // 全部归位则提示条消失
            } catch (e) {
                showToast(e.message, "错误", "error");
            } finally {
                this.migrate.busy = false;
            }
        },

        // ============================================================
        // 三平台内置 API 服务：状态机 / 启停 / 轮询
        // ============================================================

        // 徽章状态机：自定义URL > 启动中 > 未知 > 运行中/已停止
        svcBadge(key) {
            const def = svcDef(key);
            const st = this.services[key];
            if (this.form[def.customKey]) {
                return { cls: "ui-badge-info", text: "自定义URL", detail: "当前使用自定义API服务URL，内置服务已禁用" };
            }
            if (st.starting) {
                return { cls: "ui-badge-warning", text: "启动中", detail: "首次启动需自解压，最长约 1 分钟" };
            }
            const d = st.data;
            if (!d) return { cls: "ui-badge-muted", text: "未知", detail: "" };
            const detail = this.svcDetailText(d);
            if (d.running) return { cls: "ui-badge-success", text: "运行中", detail };
            return { cls: "ui-badge-danger", text: "已停止", detail };
        },

        // 详情段：端口 / 配置端口 / PID / 未找到二进制（「 · 」拼接）
        svcDetailText(d) {
            const parts = [];
            if (d.port) parts.push(`端口 ${d.port}`);
            if (d.preferred_port && d.preferred_port !== d.port) parts.push(`配置端口 ${d.preferred_port}`);
            if (d.pid) parts.push(`PID ${d.pid}`);
            if (d.bin_exists === false) parts.push("未找到二进制");
            return parts.join(" · ");
        },

        // 自定义 URL 勾选 = 内置区块锁定（禁端口/自动启动/启停操作）
        isSvcCustom(key) {
            return !!this.form[svcDef(key).customKey];
        },

        svcBusy(key) {
            const st = this.services[key];
            return st.starting || st.stopping;
        },

        // 运行中禁改端口（停止后恢复；自定义 URL 下一并锁定）
        svcPortLocked(key) {
            if (this.isSvcCustom(key)) return true;
            const d = this.services[key].data;
            return !!(d && d.running);
        },
        svcPortTitle(key) {
            if (this.isSvcCustom(key)) return "自定义URL模式下内置服务端口不生效";
            const d = this.services[key].data;
            return d && d.running ? "请先停止API服务再修改端口" : "";
        },

        async refreshSvc(key) {
            try {
                const resp = await api(`/api/${key}/status`, { timeout: 5000 });
                this.services[key].data = resp.data;
            } catch (e) {
                // 轮询失败保持上次状态即可，不打断操作
            }
        },
        refreshAllSvc() {
            SERVICE_DEFS.forEach(d => this.refreshSvc(d.key));
        },
        pollSvcStatuses() {
            if (document.hidden) return;   // 页面在后台时不发无谓请求
            this.refreshAllSvc();
        },

        async startSvc(key) {
            if (this.isSvcCustom(key)) {
                showToast("自定义URL模式下不可操作内置服务", "提示");
                return;
            }
            if (this.services[key].starting) return;
            this.services[key].starting = true;
            try {
                // 首次启动需自解压最长可达 60s，覆盖默认 15s 超时
                const resp = await api(`/api/${key}/start`, { method: "POST", timeout: 70000 });
                showToast(resp.msg, "成功", "success");
            } catch (e) {
                showToast(e.message, "错误", "error");
            } finally {
                this.services[key].starting = false;
                this.refreshSvc(key);
            }
        },

        // 停止属危险操作（该平台搜索/下载立即可用性受影响）：uiConfirm danger 二次确认
        async stopSvc(key) {
            if (this.isSvcCustom(key)) {
                showToast("自定义URL模式下不可操作内置服务", "提示");
                return;
            }
            const name = svcDef(key).name;
            const ok = await uiConfirm({
                title: `停止${name}`,
                message: `确定停止${name}吗？停止期间该平台的搜索与下载将不可用。`,
                confirmText: "停止",
                danger: true,
            });
            if (!ok) return;
            this.services[key].stopping = true;
            try {
                const resp = await api(`/api/${key}/stop`, { method: "POST" });
                showToast(resp.msg, "成功", "success");
            } catch (e) {
                showToast(e.message, "错误", "error");
            } finally {
                this.services[key].stopping = false;
                this.refreshSvc(key);
            }
        },
    };
}

// Node harness / 单测导出（浏览器端无 module 变量，此分支不生效）
if (typeof module !== "undefined" && typeof module.exports !== "undefined") {
    module.exports = { settingsPage, SERVICE_DEFS };
}
