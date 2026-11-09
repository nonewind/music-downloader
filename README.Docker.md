# Docker 部署

本目录提供 `Dockerfile` 和 `docker-compose.yml`，一键打包运行 Deen 音乐下载器。

## 国内镜像源（默认）

为加速国内构建，`Dockerfile` 默认把 apt 和 pip 源替换为：

| 类型 | 默认源 | 说明 |
|------|--------|------|
| apt | `mirrors.aliyun.com` | Debian 完整镜像，安全/CVE 覆盖一致 |
| pip | `https://mirrors.aliyun.com/pypi/simple` | 阿里云 PyPI 镜像 |

海外用户构建时如需恢复官方源：

```bash
docker build \
  --build-arg APT_MIRROR=deb.debian.org \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  -t deen-music-downloader:latest .

# compose：编辑 docker-compose.yml 中的 build.args 段（已提供注释示例）
```

## 快速开始

```bash
# 仅构建
docker compose build | tee docker-build.log

# 构建并后台启动
docker compose up -d --build

# 查看日志
docker compose logs -f

# 浏览器访问 http://<宿主机IP>:45600
# 已设置 ACCESS_TOKEN 时首次访问 http://<宿主机IP>:45600/?token=<令牌>
```

## 安全：服务没有登录

v0.9.0.0 起移除了登录体系。容器内服务监听 `0.0.0.0:45600`（容器网络要求），
`ports: "45600:45600"` 会把它暴露给**宿主机所在网络的所有人**：任何人都可以下载/删除
下载目录内的文件、修改设置。请至少做以下一项：

1. **设置访问令牌**（推荐）：在 `docker-compose.yml` 的 `environment` 段取消注释并修改
   `ACCESS_TOKEN`，然后 `docker compose up -d`。启用后所有页面与 `/api/*` 都需要令牌，
   浏览器访问一次 `http://<宿主机IP>:45600/?token=<令牌>` 后自动记住；脚本可带
   `X-Access-Token` 请求头。`/healthz`（健康检查）免令牌。
2. **只允许宿主机本机访问**：把端口映射改为 `"127.0.0.1:45600:45600"`，其他设备经反向代理
   （自带鉴权）访问。

敏感操作（导出账号 Cookie、修改自定义 API 地址/下载目录/监听地址、修改访问令牌）只对
持有有效令牌的请求开放。注意来自宿主机的请求在容器内看到的是 docker 网桥地址而非
127.0.0.1，**所以不设 `ACCESS_TOKEN` 时 Docker 部署无法在 Web 页面导出账号或修改这些设置**。

监听地址：镜像设置了 `WEB_PORT_DEFAULT=0.0.0.0:45600`，仅在新建数据库时作为 `web_port`
的初始值（本地运行的新装默认值是 `127.0.0.1:45600`，在容器里会导致端口映射不可达）；
已有 `/data` 卷里保存的 `web_port` 不受影响。也可用 `WEB_PORT` 环境变量临时覆盖（不写库）。

## 数据持久化

| 挂载点 | 用途 |
|--------|------|
| `/data` | SQLite（账号/歌单/任务/设置）+ 日志 + ncm-api 临时缓存 |
| `/app/downloads` | 下载的音乐文件 |
| `/app/logs` | 日志webapp.log |

如需把下载目录落到宿主机可见位置，编辑 `docker-compose.yml`：

```yaml
volumes:
  - /mnt/nas/deen-data:/data
  - /mnt/nas/music:/app/downloads   # 改成宿主机绝对路径
```

## 常用命令

```bash
docker compose ps              # 查看容器状态
docker compose restart         # 重启
docker compose pull && docker compose up -d   # 升级镜像
docker compose down            # 停止并删除容器（保留数据卷）
```

## 修改配置

大部分配置（端口、定时同步、音质、下载路径等）通过 Web UI 修改即可，无需重启容器。
**例外**：`web_port`（监听地址）修改后需要重启（容器内必须保持 `0.0.0.0`，对外暴露范围请用
端口映射控制）：

```bash
docker compose restart
```

## 直接使用 docker（不通过 compose）

```bash
# 构建
docker build -t chongya369/deen-music-downloader:latest .

# 运行
docker run -d --name deen \
  -p 45600:45600 \
  -v ./deen-data:/data \
  -v ./deen-downloads:/app/downloads \
  -e TZ=Asia/Shanghai \
  -e ACCESS_TOKEN=please-change-me \
  --restart unless-stopped \
  chongya369/deen-music-downloader:latest
```

默认以 `deen`（UID/GID 1000）非 root 用户运行。
