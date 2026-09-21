# GitHub CI/CD 与公司内网部署

## 架构

```text
push / pull request
        |
        v
GitHub-hosted runner: 安装依赖、测试、迁移冒烟检查
        |
        | main / cicd 测试通过
        v
公司 Linux self-hosted runner: 本机原子发布、迁移、重启、健康检查
        |
        v
Nginx -> FastAPI -> PostgreSQL/SQLite
```

内网主机不需要接受 GitHub 的入站连接。Self-hosted runner 主动通过 HTTPS 443
连接 GitHub 领取任务。运行数据库、导出文件和 `app.env` 不会上传到 GitHub。

如果内网主机不能访问 GitHub，请改用 Actions Artifact 离线交付，见
[`OFFLINE_DEPLOYMENT.md`](OFFLINE_DEPLOYMENT.md)。自动部署默认关闭，只有仓库变量
`ENABLE_SELF_HOSTED_DEPLOY=true` 时才会向 self-hosted runner 投递部署任务。

## 1. Linux 前置条件

- 具有 systemd 的 x86_64 Linux。
- Python 3.12、`python3.12-venv`、Git、curl、rsync、Nginx。
- 主机可以访问 GitHub HTTPS；安装 Python 依赖时还需访问 PyPI 或公司内部镜像。
- 推荐 PostgreSQL。SQLite 只适用于单主机、单 Web worker。

以 Debian/Ubuntu 为例：

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git curl rsync nginx
sudo useradd --create-home --shell /bin/bash github-runner
```

先将仓库临时克隆到主机，然后执行一次初始化：

```bash
git clone https://github.com/OWNER/REPOSITORY.git
cd REPOSITORY
sudo RUNNER_USER=github-runner bash deploy/bootstrap.sh
sudo editor /etc/commodity-research-platform/app.env
```

初始化脚本会创建：

- 应用版本目录：`/opt/commodity-research-platform/releases`
- 当前版本链接：`/opt/commodity-research-platform/current`
- 持久数据目录：`/var/lib/commodity-research-platform`
- 服务配置：`/etc/commodity-research-platform/app.env`
- Web 服务和每日 08:00、16:30 采集 timer
- runner 仅重启本项目服务所需的最小 sudo 权限

初始化后重新登录 `github-runner`，使新增用户组生效。

## 2. 注册内网 Runner

进入 GitHub 仓库：`Settings -> Actions -> Runners -> New self-hosted runner`，选择 Linux x64，
然后以 `github-runner` 用户执行页面给出的下载命令。配置命令必须增加生产标签：

```bash
./config.sh --url https://github.com/OWNER/REPOSITORY \
  --token GITHUB_GENERATED_TOKEN \
  --labels commodity-production \
  --name commodity-production-01 \
  --unattended
sudo ./svc.sh install github-runner
sudo ./svc.sh start
```

注册 token 是 GitHub 页面临时生成的短期 token，不要写入仓库或 Actions secrets。
runner 在线后应显示标签 `self-hosted`、`linux`、`x64`、`commodity-production`。

## 3. GitHub 仓库设置

1. 在 `Settings -> Environments` 创建 `production`。
2. 将 deployment branches 限制为 `main` 和 `cicd`。
3. 可设置变量 `PRODUCTION_URL`，例如 `https://commodity-research.internal`。
4. 设置变量 `ENABLE_SELF_HOSTED_DEPLOY=true` 以开启 runner 自动部署。
5. 在分支保护中要求 `Test` 通过后才能合并到 `main` 或 `cicd`。
6. 如果必须完全自动部署，不配置 required reviewer；需要人工放行时再启用它。

部署不需要 SSH 私钥或数据库密码存入 GitHub。数据库配置只保存在内网主机的
`/etc/commodity-research-platform/app.env`。

## 4. Nginx 与内网访问

修改 `deploy/nginx/commodity-research-platform.conf` 中的内部域名，然后安装：

```bash
sudo cp deploy/nginx/commodity-research-platform.conf /etc/nginx/conf.d/
sudo nginx -t
sudo systemctl reload nginx
```

生产环境建议使用公司 CA 签发的 TLS 证书。FastAPI 仅监听 `127.0.0.1:8765`，
不直接暴露给办公网络或公网。

## 5. 发布过程

向 `main` 或 `cicd` 推送后 `.github/workflows/ci-deploy.yml` 自动执行：

1. GitHub runner 运行全部测试和 Alembic 冒烟检查。
2. 内网 runner 将代码复制到新的 release 目录。
3. 创建独立 venv 并安装生产依赖。
4. 执行 Alembic 升级和幂等种子初始化。
5. 原子切换 `current` 链接并重启 systemd 服务。
6. 请求 `/healthz`，验证进程和数据库都正常。
7. 健康检查失败时恢复上一版本并重启。
8. 保留最近五个 release。

数据库迁移通常只应采用向后兼容的“先加后删”方式。代码可以自动回切，但破坏性数据库
迁移不能靠代码回切恢复，必须先备份并单独安排。

## 6. 运维命令

```bash
sudo systemctl status commodity-research-platform
sudo journalctl -u commodity-research-platform -f

sudo systemctl status commodity-research-platform-daily.timer
sudo systemctl list-timers commodity-research-platform-daily.timer
sudo systemctl start commodity-research-platform-daily.service

curl --fail http://127.0.0.1:8765/healthz
readlink -f /opt/commodity-research-platform/current
```

## 7. 网络和安全边界

- runner 使用专用低权限账户，不要使用 root 运行。
- 只允许受保护的 `main`、`cicd` 分支触发生产部署。
- 不要在可由外部贡献者修改的工作流上使用这台生产 runner。
- 数据库只监听内网或 localhost，浏览器只能通过 FastAPI 访问数据。
- 订阅和授权数据保留在内网数据库，不写入 Git 仓库或 Actions artifact。
- 对 PostgreSQL、`/var/lib/commodity-research-platform` 和环境文件定期备份。
