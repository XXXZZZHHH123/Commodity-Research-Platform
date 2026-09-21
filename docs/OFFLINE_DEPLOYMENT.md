# GitHub Artifact 容器化离线交付

当公司 Linux 无法访问 GitHub 或 PyPI 时，由 GitHub Actions 构建完整容器镜像，再通过
Artifact 人工传入内网。镜像已经包含 Python 3.12、Python 依赖和应用代码，服务器不需要
安装 Python，也不需要 GitHub runner。

## 交付边界

服务器只需满足：

- x86_64 Linux 和 systemd
- 可正常运行的 Docker Engine
- 能访问业务数据源；如果完全不能出网，网页可以运行，但自动采集会失败

数据库、导出文件和配置不写入镜像，分别保存在：

```text
/var/lib/commodity-research-platform
/etc/commodity-research-platform/app.env
```

升级或删除容器不会删除这些数据。

## 1. 下载与传输

向 `main` 或 `cicd` 推送后，Actions 中的 `Build offline container bundle` 任务生成：

```text
commodity-research-container-linux-amd64-<commit>.tar.gz
commodity-research-container-linux-amd64-<commit>.tar.gz.sha256
```

从 GitHub Actions 的 Artifacts 下载外层 zip，解压后将上述两个文件复制到服务器，例如：

```bash
scp commodity-research-container-linux-amd64-*.tar.gz* root@INTERNAL_SERVER:/opt/delivery/
```

## 2. 校验与解压

在服务器执行：

```bash
cd /opt/delivery
sha256sum -c commodity-research-container-linux-amd64-*.tar.gz.sha256
tar -xzf commodity-research-container-linux-amd64-*.tar.gz
cd commodity-research-container-linux-amd64-*
```

校验必须显示 `OK`。

## 3. 首次安装或升级

```bash
bash install.sh
```

第一次执行会创建默认配置。当前默认 SQLite 配置可以直接运行：

```text
TIN_DATABASE_URL=sqlite:////var/lib/commodity-research-platform/data/tin.db
```

需要 PostgreSQL 时，先编辑配置，再重新启动容器：

```bash
vi /etc/commodity-research-platform/app.env
systemctl restart commodity-research-platform
```

安装脚本会导入镜像、迁移数据库、启动网页服务和定时采集，并等待容器健康检查通过。
升级失败时会恢复上一个 `current` 镜像。

## 4. 验证与运维

```bash
docker ps --filter name=commodity-research-platform
docker logs --tail 100 commodity-research-platform
systemctl status commodity-research-platform
systemctl status commodity-research-platform-daily.timer
curl --fail http://127.0.0.1:8765/healthz
```

手动执行一次采集：

```bash
systemctl start commodity-research-platform-daily.service
journalctl -u commodity-research-platform-daily.service -n 100 --no-pager
```

Nginx 仍运行在宿主机并反向代理 `127.0.0.1:8765`。

## 5. 安全与备份

- 容器使用非 root 用户、只读根文件系统、移除 Linux capabilities。
- 仅持久数据目录可写；配置由 Docker 在启动时读取，不打入镜像。
- 定期备份 `/var/lib/commodity-research-platform` 和 `app.env`。
- SQLite 只适合单主机单 Web 实例；多人并发或扩容时切换 PostgreSQL。
