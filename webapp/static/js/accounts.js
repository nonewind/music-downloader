// 账号管理页（Frontend 2.0 重写，UI-03c）
//
// 布局：页头（导入 / 导出[仅 admin，模板条件渲染] / 添加账号）+ 平台过滤
// 分段（全部/网易云/QQ/酷狗 + 计数徽章）。「全部」= 卡片只读视图（平台分组/
// VIP/到期时间（过期红、酷狗未提供灰）/启停状态/月额度）；单平台 = 表格可
// 编辑视图（上移/下移（首尾禁用）、启停开关、测试登录、编辑弹窗、删除）。
//
// 依赖全局工具（app.js 先于本脚本加载）：api()、showToast()、uiConfirm()、
// window.PLATFORM_META。渲染全部由 Alpine 模板绑定（x-for/x-text/:class），
// 本文件不拼任何 innerHTML。
//
// API 契约与旧页逐字节对齐：
//   GET    /api/accounts                列表（含 monthly_downloaded）
//   POST   /api/accounts                {platform,name,cookie,quota_limit}
//   PUT    /api/accounts/<aid>          {enabled} / {name,quota_limit[,cookie]}（cookie 空=不改）
//   DELETE /api/accounts/<aid>          删除（已下载歌曲不受影响，由后端解绑）
//   POST   /api/accounts/<aid>/move     {direction: "up"|"down"}
//   POST   /api/accounts/<aid>/test     测试登录（酷狗成功文案由后端拼「下载实测」说明）
//   POST   /api/accounts/import         JSON body（文件内容原样转发，与旧页一致非 FormData）
//   GET    /api/accounts/export         仅管理员；window.location 直跳必须拼 APP_BASE
//   POST   /api/{ncm|qq|kugou}/qr/create|check  扫码登录（create 70s / check 30s 超时；
//          2.5s 轮询；status 酷狗语义 0=过期 1=等待扫码 2=已扫码待确认 3=拒绝 4=成功含 cookie）
//
// 三个扫码轮询 timer 在组件内统一管理（_qrTimers 槽位）：切换平台、关闭
// 添加弹窗（$watch $store.modals.addAcc，替代旧 hidden.bs.modal 语义）、
// 组件销毁（Alpine destroy 钩子）时全部清理；页面整页跳转无需额外清理。

// 扫码登录 API 前缀（后端路由：ncm = 网易云）
const QR_API_BASE = { netease: "ncm", qq: "qq", kugou: "kugou" };

// 平台 -> Cookie 粘贴区 {placeholder, hint}（含三平台核心字段校验提示：
// 网易云 MUSIC_U / QQ uin 或 musicid / 酷狗 token；后端为最终校验方）
const ADD_PLATFORM_HINTS = {
    netease: {
        placeholder: "MUSIC_U=xxxx; os=pc（也可点下方扫码自动填入）",
        hint: "网易云：点下方「扫码登录」用网易云APP扫码自动填入；或浏览器登录 music.163.com → F12 → Application → Cookies → 复制 MUSIC_U",
    },
    qq: {
        placeholder: "uin=xxxx; eas_sid=xxxx; ...（完整 Cookie，也可点下方扫码自动填入）",
        hint: "QQ音乐：点下方「QQ扫码登录/微信扫码登录」自动填入；或浏览器登录 y.qq.com → F12 → Network → 任选 y.qq.com 请求 → 复制完整 Cookie（须含 uin，昵称还需 eas_sid）",
    },
    kugou: {
        placeholder: "token=xxx;userid=xxx（也可点下方扫码自动填入）",
        hint: "酷狗音乐：点下方「扫码登录」用酷狗APP扫码自动填入；或手动填入 token=xxx;userid=xxx",
    },
};

// 扫码成功 toast 文案（与旧页一致）
const QR_SUCCESS_TOAST = {
    netease: "网易云扫码登录成功，Cookie 已填入",
    qq: "QQ音乐扫码登录成功，Cookie 已填入",
    kugou: "酷狗扫码登录成功，Cookie 已填入",
};

function accountsPage() {
    return {
        // ---- 账号数据 ----
        accounts: [],
        loading: true,

        // ---- 平台过滤分段（'all' = 卡片只读视图，单平台 = 表格可编辑视图） ----
        platform: "all",

        // ---- 行内操作 ----
        testBusy: new Set(),   // 测试登录进行中的账号 id（按钮防重 + 行内 loading）

        // ---- 添加账号弹窗 ----
        addForm: { platform: "netease", name: "", cookie: "", quota_limit: 0 },
        addBusy: false,

        // ---- 编辑账号弹窗（打开前重新 GET /api/accounts 取最新） ----
        editForm: { id: 0, platform: "", platformLabel: "", name: "", cookie: "", quota_limit: 0 },
        editBusy: false,

        // ---- 扫码登录（三平台共用一套状态；同一时刻至多一个会话） ----
        // status: idle=未开始 creating=二维码生成中 polling=等待/已扫码
        //         success=成功已填入 rejected=用户拒绝 expired=已过期 error=生成失败/查询中
        qr: { platform: "", loginType: "", status: "idle", qrImg: "", message: "" },

        // 三个扫码轮询 timer 槽位 + 会话凭据（组件内统一管理；Alpine 会在
        // 每次组件初始化时调用 accountsPage()，字面量随实例新建，无跨页共享）
        _qrTimers: { netease: null, qq: null, kugou: null },
        _qrRefs: { netease: {}, qq: {}, kugou: {} },

        // ============================================================
        // 初始化 / 销毁
        // ============================================================
        async init() {
            // 添加弹窗关闭钩子（旧 hidden.bs.modal 语义）：统一停全部扫码轮询
            this.$watch("$store.modals.addAcc", (open) => { if (!open) this.stopAllQrPolling(); });
            await this.loadAccounts();
            this.syncSubTab();
        },

        // 组件销毁兜底清理（页面整页跳转无需，SPA 式复用时防 timer 泄漏）
        destroy() {
            this.stopAllQrPolling();
        },

        // ============================================================
        // 数据加载与过滤
        // ============================================================
        async loadAccounts() {
            this.loading = true;
            try {
                const data = await api("/api/accounts");
                this.accounts = data.data || [];
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.loading = false;
            }
        },

        get platformList() {
            const meta = window.PLATFORM_META || {};
            return Object.keys(meta).map(k => ({ key: k, name: (meta[k] && meta[k].name) || k }));
        },
        get platformTabs() {
            return [{ key: "all", name: "全部" }, ...this.platformList];
        },
        get addPlatformOptions() {
            return this.platformList;
        },

        get counts() {
            const c = { all: this.accounts.length, netease: 0, qq: 0, kugou: 0 };
            this.accounts.forEach(a => { if (c[a.platform] !== undefined) c[a.platform]++; });
            return c;
        },
        get filteredAccounts() {
            return this.platform === "all"
                ? this.accounts
                : this.accounts.filter(a => a.platform === this.platform);
        },
        // 卡片视图按平台分组（固定 netease → qq → kugou，空平台不显示）
        get platformGroups() {
            return ["netease", "qq", "kugou"]
                .map(p => ({ platform: p, items: this.accounts.filter(a => a.platform === p) }))
                .filter(g => g.items.length > 0);
        },

        switchPlatform(p) {
            if (this.platform === p) return;
            this.platform = p;
            this.syncSubTab();
        },

        // 顶栏二级面包屑跟随分段（旧页 setSubTab 语义）
        syncSubTab() {
            if (window.AppUI && window.AppUI.setSubTab) {
                window.AppUI.setSubTab(this.platform === "all" ? "全部" : this.platName(this.platform));
            }
        },

        // ============================================================
        // 展示辅助（模板经 x-text/:class 消费，零 innerHTML）
        // ============================================================
        platName(p) {
            const meta = window.PLATFORM_META || {};
            return (meta[p] && meta[p].name) || p;
        },
        platBadgeCls(p) {
            return (window.PLATFORM_META && window.PLATFORM_META[p]) ? "ui-badge-plat-" + p : "ui-badge-muted";
        },
        addHint(p) {
            return ADD_PLATFORM_HINTS[p] || ADD_PLATFORM_HINTS.netease;
        },
        // 月额度：上限 > 0 显示「已用 / 上限」；不限额省略上限（「N（不限）」）
        quotaText(a) {
            return a.quota_limit > 0
                ? `${a.monthly_downloaded} / ${a.quota_limit}`
                : `${a.monthly_downloaded}（不限）`;
        },
        quotaCls(a) {
            return (a.quota_limit > 0 && a.monthly_downloaded >= a.quota_limit)
                ? "acct-text-danger" : "acct-text-ok";
        },
        // 到期时间：过期红 / 未过期绿 / 酷狗未提供灰 / 非会员「—」
        expireMeta(a) {
            if (!a.vip_type || a.vip_type <= 0) return { text: "—", cls: "ui-text-3" };
            if (!a.vip_expire_at) {
                // 酷狗上游 /user/detail 未返回到期时间字段（vip_expire_ts 恒 0）
                return a.platform === "kugou"
                    ? { text: "酷狗未提供", cls: "ui-text-3" }
                    : { text: "未知", cls: "ui-text-3" };
            }
            const d = a.vip_expire_at.split(" ")[0];
            const today = new Date();
            today.setHours(0, 0, 0, 0);
            const expired = new Date(d) < today;
            return { text: d, cls: expired ? "acct-text-danger" : "acct-text-ok" };
        },

        // ============================================================
        // 表格视图行操作
        // ============================================================

        // 启用/禁用（PUT {enabled}；失败回滚 = 重载真实状态）
        async toggleEnabled(a, on) {
            try {
                await api(`/api/accounts/${a.id}`, {
                    method: "PUT",
                    body: JSON.stringify({ enabled: on }),
                });
                showToast(on ? "已启用" : "已禁用");
            } catch (e) {
                showToast(e.message, "错误");
                this.loadAccounts();
            }
        },

        // 上移/下移（首尾禁用由模板 :disabled 控制；后端在同平台内交换）
        async moveAccount(a, direction) {
            try {
                await api(`/api/accounts/${a.id}/move`, {
                    method: "POST",
                    body: JSON.stringify({ direction }),
                });
                await this.loadAccounts();
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // 测试登录（酷狗成功文案由后端拼接，含「下载实测」能力说明）
        async testLogin(a) {
            if (this.testBusy.has(a.id)) return;
            this.testBusy = new Set([...this.testBusy, a.id]);
            try {
                const data = await api(`/api/accounts/${a.id}/test`, { method: "POST" });
                showToast(data.msg, "登录测试");
                await this.loadAccounts();   // 测试会刷新昵称/会员信息，重载展示
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                const next = new Set(this.testBusy);
                next.delete(a.id);
                this.testBusy = next;
            }
        },

        // 编辑：打开前重新 GET /api/accounts 取最新（避免拿过期数据编辑）
        async openEdit(a) {
            try {
                const data = await api("/api/accounts");
                const acc = (data.data || []).find(x => x.id === a.id);
                if (!acc) return;
                this.editForm = {
                    id: acc.id,
                    platform: acc.platform,
                    platformLabel: acc.platform_name || this.platName(acc.platform),
                    name: acc.name,
                    cookie: "",              // 留空 = 不修改
                    quota_limit: acc.quota_limit,
                };
                this.$store.modals.editAcc = true;
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // 编辑保存：payload 只带 name/quota_limit；Cookie 非空才附带给后端
        async confirmEdit() {
            const f = this.editForm;
            const name = (f.name || "").trim();
            if (!name) { showToast("请填写账号别名"); return; }
            const payload = { name, quota_limit: parseInt(f.quota_limit) || 0 };
            const cookie = (f.cookie || "").trim();
            if (cookie) payload.cookie = cookie;
            this.editBusy = true;
            try {
                await api(`/api/accounts/${f.id}`, {
                    method: "PUT",
                    body: JSON.stringify(payload),
                });
                showToast("已保存");
                this.$store.modals.editAcc = false;
                await this.loadAccounts();
            } catch (e) {
                showToast(e.message, "错误");
            } finally {
                this.editBusy = false;
            }
        },

        // 删除（已下载的歌曲不受影响）
        async confirmDelete(a) {
            const ok = await uiConfirm({
                title: "删除账号",
                message: `确定删除账号「${a.name}」吗？（已下载的歌曲不受影响）`,
                confirmText: "删除",
                danger: true,
            });
            if (!ok) return;
            try {
                await api(`/api/accounts/${a.id}`, { method: "DELETE" });
                showToast("已删除");
                await this.loadAccounts();
            } catch (e) {
                showToast(e.message, "错误");
            }
        },

        // ============================================================
        // 导入 / 导出
        // ============================================================
        pickImportFile() {
            this.$refs.importFile.click();
        },

        // 文件选择 → 数量确认（同平台+同名跳过提示）→ POST import（JSON body）
        async onImportFile(e) {
            const input = e.target;
            const file = input.files && input.files[0];
            if (!file) return;
            try {
                const text = await file.text();
                const payload = JSON.parse(text);
                const count = (payload.accounts || []).length;
                if (count === 0) {
                    showToast("文件中没有可导入的账号");
                    return;
                }
                const ok = await uiConfirm({
                    title: "导入账号",
                    message: `即将导入 ${count} 个账号（同平台+同名账号将自动跳过）。是否继续？`,
                    confirmText: "导入",
                });
                if (!ok) return;
                const data = await api("/api/accounts/import", {
                    method: "POST",
                    body: JSON.stringify(payload),
                });
                showToast(data.msg, "导入结果");
                await this.loadAccounts();
            } catch (err) {
                showToast("导入失败：" + err.message, "错误");
            } finally {
                input.value = "";   // 允许重复选择同一文件
            }
        },

        // 导出（按钮仅 admin 可见，模板条件渲染）：敏感提示确认后直跳下载；
        // 网关前缀下直接跳转绝对路径会脱前缀，必须拼接 APP_BASE
        async exportClick() {
            const ok = await uiConfirm({
                title: "导出账号",
                message: "导出文件包含 Cookie 等敏感信息，请妥善保管。是否继续？",
                confirmText: "导出",
            });
            if (!ok) return;
            window.location.href = (window.APP_BASE || "") + "/api/accounts/export";
        },

        // ============================================================
        // 添加账号弹窗
        // ============================================================

        // 打开：平台默认选中当前过滤平台（「全部」则默认 netease，与旧页一致）
        openAdd() {
            const platform = this.platform !== "all" ? this.platform : "netease";
            this.addForm = { platform, name: "", cookie: "", quota_limit: 0 };
            this.stopAllQrPolling();
            this.$store.modals.addAcc = true;
            // 程序赋值不触发 @change：placeholder/提示文案经 addHint(addForm.platform)
            // 响应式绑定自动刷新，无需手动同步
        },

        // 手动切换平台 → 停掉原平台扫码会话（提示文案随绑定自动切换）
        onAddPlatformChange(p) {
            this.addForm.platform = p;
            this.stopAllQrPolling();
        },

        async confirmAdd() {
            const f = this.addForm;
            const name = (f.name || "").trim();
            const cookie = (f.cookie || "").trim();
            if (!name) { showToast("请填写账号别名"); return; }
            if (!cookie) { showToast("请填写 Cookie"); return; }
            this.addBusy = true;
            try {
                const data = await api("/api/accounts", {
                    method: "POST",
                    body: JSON.stringify({
                        platform: f.platform, name, cookie,
                        quota_limit: parseInt(f.quota_limit) || 0,
                    }),
                });
                showToast(data.msg, "添加成功");
                this.$store.modals.addAcc = false;   // 关闭钩子统一停扫码轮询
                this.addForm = { platform: f.platform, name: "", cookie: "", quota_limit: 0 };
                await this.loadAccounts();
            } catch (e) {
                showToast(e.message, "添加失败");
            } finally {
                this.addBusy = false;
            }
        },

        // ============================================================
        // 扫码登录（三平台状态机：create 70s → 2.5s 轮询 check 30s）
        // ============================================================
        qrCreating(p) {
            return this.qr.status === "creating" && this.qr.platform === p;
        },
        qrWaitMessage(p, loginType) {
            if (p === "qq") return loginType === "wx" ? "请使用微信扫描二维码登录" : "请使用手机QQ扫描二维码登录";
            if (p === "kugou") return "请使用酷狗音乐APP扫描二维码登录";
            return "请使用网易云音乐APP扫描二维码登录";
        },

        // QQ 平台双按钮（QQ / 微信）共用一套面板与轮询，login_type 区分
        async startQrLogin(platform, loginType = "") {
            // 先清理全部残留轮询（含旧会话凭据与展示态），再开新会话
            this.stopAllQrPolling();
            this.qr = { platform, loginType, status: "creating", qrImg: "", message: "正在生成二维码，请稍候…" };
            try {
                // 冷启内置 api 服务最长可达 60s（onefile 解压），覆盖默认 15s
                const opts = { method: "POST", timeout: 70000 };
                if (platform === "qq") opts.body = JSON.stringify({ login_type: loginType });
                const data = await api(`/api/${QR_API_BASE[platform]}/qr/create`, opts);
                if (platform === "qq") this._qrRefs.qq = { identifier: data.data.identifier, loginType };
                else this._qrRefs[platform] = { key: data.data.key };
                this.qr.qrImg = data.data.qr_img;
                this.qr.status = "polling";
                this.qr.message = this.qrWaitMessage(platform, loginType);
                // 2.5s 轮询扫码状态
                this._qrTimers[platform] = setInterval(() => this.checkQrOnce(platform), 2500);
            } catch (e) {
                showToast(e.message, "错误");
                this.qr.status = "error";
                this.qr.message = "二维码生成失败：" + e.message;
            }
        },

        async checkQrOnce(platform) {
            if (!this._qrTimers[platform]) return;   // 轮询已被清理（弹窗关闭/切换）
            const ref = this._qrRefs[platform] || {};
            const body = platform === "qq"
                ? { login_type: ref.loginType, identifier: ref.identifier }
                : { key: ref.key };
            try {
                const resp = await api(`/api/${QR_API_BASE[platform]}/qr/check`, {
                    method: "POST",
                    body: JSON.stringify(body),
                    timeout: 30000,
                });
                if (!this._qrTimers[platform]) return;   // 等待期间被清理：丢弃过期响应
                const st = resp.data.status;
                if (st === 1) {
                    this.qr.status = "polling";
                    this.qr.message = "等待扫码…";
                } else if (st === 2) {
                    this.qr.status = "polling";
                    this.qr.message = "已扫码，请在手机上确认登录";
                } else if (st === 4) {
                    // 成功：停轮询、保留面板提示，Cookie 自动填入添加表单
                    this.stopQrPolling(platform, true);
                    this.addForm.cookie = resp.data.cookie || "";
                    this.qr.status = "success";
                    this.qr.message = "✅ 登录成功，Cookie 已自动填入";
                    showToast(QR_SUCCESS_TOAST[platform] || "扫码登录成功，Cookie 已填入", "成功");
                } else if (st === 3) {
                    // 酷狗「取消授权」/ QQ「用户拒绝授权」（网易云无此态）
                    this.stopQrPolling(platform, true);
                    this.qr.status = "rejected";
                    this.qr.message = platform === "kugou"
                        ? "用户已取消授权，请重新点击「扫码登录」"
                        : "用户已拒绝授权，请重新点击「扫码登录」";
                } else if (st === 0) {
                    // 过期：仅停轮询、保留面板与提示（隐藏面板会导致提示写到
                    // 不可见元素上，用户什么都看不到——旧页网易云分支语义，三平台统一）
                    this.stopQrPolling(platform, true);
                    this.qr.status = "expired";
                    this.qr.message = "二维码已过期，请重新点击「扫码登录」";
                }
            } catch (e) {
                // 轮询失败（服务重启中/网络抖动）：可见提示，下一轮继续
                this.qr.message = `状态查询失败（${e.message}），重试中…`;
            }
        },

        // 停单个平台轮询：keepPanel=true 时保留二维码与提示（成功/拒绝/过期态）
        stopQrPolling(platform, keepPanel = false) {
            if (this._qrTimers[platform]) {
                clearInterval(this._qrTimers[platform]);
                this._qrTimers[platform] = null;
            }
            this._qrRefs[platform] = {};
            if (!keepPanel) {
                this.qr = { platform: "", loginType: "", status: "idle", qrImg: "", message: "" };
            }
        },

        // 统一停全部扫码轮询（切换平台 / 关闭弹窗 / 组件销毁共用）
        stopAllQrPolling() {
            ["netease", "qq", "kugou"].forEach(p => {
                if (this._qrTimers[p]) {
                    clearInterval(this._qrTimers[p]);
                    this._qrTimers[p] = null;
                }
                this._qrRefs[p] = {};
            });
            this.qr = { platform: "", loginType: "", status: "idle", qrImg: "", message: "" };
        },
    };
}
