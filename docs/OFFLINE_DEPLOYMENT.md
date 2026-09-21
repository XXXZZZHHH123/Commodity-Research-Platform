# GitHub Artifact 容器化离线交付

当公司 Linux 无法访问 GitHub 或 PyPI 时，由 GitHub Actions 构建完整容器镜像，再通过
Artifact 人工传入内网。应用镜像包含 Python 3.12、全部 Python 依赖和应用代码，服务器
不需要安装 Python 或 GitHub runner。Nginx 属于主机基础设施，只需单独安装一次，不随
每个应用版本重复交付。

## 交付边界

服务器只需满足：

- x86_64 Linux 和 systemd
- 可正常运行的 Docker Engine
- 宿主机已安装 Nginx；CentOS 7 离线安装方法见下文
- 宿主机时区建议设置为 `Asia/Shanghai`，定时采集按宿主机本地时间执行
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

安装脚本只导入应用镜像、迁移数据库、启动网页服务和定时采集，并等待应用容器健康检查
通过。升级失败时会恢复上一个 `current` 镜像。应用只监听 `127.0.0.1:8765`，由宿主机
Nginx 监听 80 端口。

## 4. 验证与运维

```bash
docker ps --filter name=commodity-research-platform
docker logs --tail 100 commodity-research-platform
systemctl status commodity-research-platform
systemctl status commodity-research-platform-daily.timer
curl --fail http://127.0.0.1:8765/healthz
curl --fail http://127.0.0.1/healthz
```

手动执行一次采集：

```bash
systemctl start commodity-research-platform-daily.service
journalctl -u commodity-research-platform-daily.service -n 100 --no-pager
```

## 5. CentOS 7 一次性安装 Nginx

CentOS 7 已停止维护，普通 yum 镜像不再提供完整仓库。Nginx 1.26 依赖 CentOS 7 基础
仓库中的 PCRE2 运行库，因此需要在联网电脑同时下载 PCRE2、Nginx 官方归档 RPM 和
签名密钥，再复制到服务器：

```bash
curl -fLO https://vault.centos.org/7.9.2009/os/x86_64/Packages/pcre2-10.23-2.el7.x86_64.rpm
curl -fLO https://nginx.org/packages/centos/7/x86_64/RPMS/nginx-1.26.1-2.el7.ngx.x86_64.rpm
curl -fLO https://nginx.org/keys/nginx_signing.key
scp pcre2-10.23-2.el7.x86_64.rpm nginx-1.26.1-2.el7.ngx.x86_64.rpm \
  nginx_signing.key root@SERVER:/opt/delivery/
```

在服务器安装并配置：

```bash
rpm --import /opt/delivery/nginx_signing.key
rpm -K /opt/delivery/pcre2-10.23-2.el7.x86_64.rpm
rpm -K /opt/delivery/nginx-1.26.1-2.el7.ngx.x86_64.rpm
rpm -Uvh /opt/delivery/pcre2-10.23-2.el7.x86_64.rpm
rpm -Uvh /opt/delivery/nginx-1.26.1-2.el7.ngx.x86_64.rpm
mv /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf.disabled 2>/dev/null || true
cp nginx/commodity-research-platform.conf /etc/nginx/conf.d/
setsebool -P httpd_can_network_connect 1
nginx -t
systemctl enable nginx
systemctl restart nginx
```

如果启用了 firewalld，放行 HTTP：

```bash
firewall-cmd --permanent --add-service=http
firewall-cmd --reload
```

办公电脑通过 `http://SERVER_IP` 访问。后续应用升级不会重新安装或覆盖 Nginx。

## 6. 安全与备份

- 应用容器使用非 root 用户、只读根文件系统，并移除 Linux capabilities。
- 仅持久数据目录可写；配置由 Docker 在启动时读取，不打入镜像。
- 定期备份 `/var/lib/commodity-research-platform` 和 `app.env`。
- SQLite 只适合单主机单 Web 实例；多人并发或扩容时切换 PostgreSQL。
